"""Bounded command supervision within the controller's process/cgroup.

Web/API clients request only allowlisted saved tasks or Compose actions.
No raw argv, Docker socket or host shell is available over controller IPC.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from uuid import uuid4

from devstack.domain.models import project_id
from .coordinator import ControlError

_ALLOWED_COMPOSE = {"build", "restart"}


class JobError(RuntimeError):
    pass


class JobSupervisor:
    def __init__(self, coordinator, backend, workspaces):
        self.coordinator = coordinator
        self.backend = backend
        self.workspaces = workspaces
        self.database = coordinator.database
        self._guard = threading.Lock()
        self._running: dict[str, tuple[threading.Thread, subprocess.Popen | None]] = {}
        coordinator.jobs = self

    def _task(self, identifier: str, task_name: str) -> list[str]:
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT service,mode,image,network,argv_json FROM tasks "
                "WHERE project_id=? AND name=?",
                (identifier, task_name),
            ).fetchone()
        if row is None:
            raise JobError("Unknown saved task")
        data = dict(row)
        data["argv"] = json.loads(data.pop("argv_json"))
        return self.backend.task_command(identifier, data)

    def submit_task(self, identifier: str, task_name: str) -> str:
        return self._submit(identifier, "task", task_name)

    def submit_compose(self, identifier: str, action: str) -> str:
        if action not in _ALLOWED_COMPOSE:
            raise JobError("Unsupported Compose action")
        return self._submit(identifier, "compose", action)

    def _submit(self, identifier: str, kind: str, action: str) -> str:
        project_id(identifier)
        with self.coordinator.locked():
            state = self.coordinator.status()
            self.coordinator.admit(identifier, state["generation"] or "")
            argv = (self._task(identifier, action) if kind == "task"
                    else self.backend.command(identifier) + [action])
            if not all(isinstance(arg, str) and arg for arg in argv):
                raise JobError("Invalid worker command")
            with self.database.transaction() as con:
                existing = con.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()
            if existing:
                raise JobError("A project command is already in progress")
            job_id = str(uuid4())
            with self.database.transaction(write=True) as con:
                con.execute(
                    "INSERT INTO jobs(id,project_id,generation,kind,status,"
                    "action,started_ns) VALUES (?,?,?,?,?,?,?)",
                    (job_id, identifier, state["generation"], kind, "queued",
                     action, time.time_ns()),
                )
            thread = threading.Thread(
                target=self._worker, args=(job_id, identifier, state["generation"],
                                           tuple(argv)), daemon=True,
                name="devstack-job-" + job_id[:8],
            )
            with self._guard:
                self._running[job_id] = (thread, None)
            thread.start()
            return job_id

    def _set(self, job_id: str, status: str, exit_code: int | None = None) -> None:
        with self.database.transaction(write=True) as con:
            con.execute(
                "UPDATE jobs SET status=?,exit_code=?,finished_ns=? WHERE id=?",
                (status, exit_code, None if status == "running" else time.time_ns(),
                 job_id),
            )

    def _state(self, job_id: str) -> str:
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,),
            ).fetchone()
        return row["status"] if row is not None else "cancelled"

    def _worker(self, job_id: str, identifier: str, generation: str,
                argv: tuple[str, ...]) -> None:
        proc = None
        try:
            # Never execute a queued command after a switch or reactivation.
            with self.coordinator.locked():
                if self._state(job_id) != "queued":
                    return
                if not self.coordinator.authorized(identifier, generation):
                    self._set(job_id, "cancelled")
                    return
                logs = self.workspaces.path(identifier) / "logs"
                log_path = logs / (job_id + ".log")
                if log_path.is_symlink() or log_path.exists():
                    raise JobError("Unsafe job log destination")
                with log_path.open("xb") as out:
                    log_path.chmod(0o600)
                    proc = subprocess.Popen(
                        list(argv), stdin=subprocess.DEVNULL,
                        stdout=out, stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                with self._guard:
                    self._running[job_id] = (threading.current_thread(), proc)
                self._set(job_id, "running")
            try:
                exit_code = proc.wait(timeout=900)
                final = "succeeded" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired:
                self._terminate(proc)
                final, exit_code = "failed", None
            if self._state(job_id) != "cancelled":
                self._set(job_id, final, exit_code)
        except Exception:
            if proc is not None and proc.poll() is None:
                self._terminate(proc)
            if self._state(job_id) != "cancelled":
                self._set(job_id, "failed")
        finally:
            with self._guard:
                self._running.pop(job_id, None)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise JobError("Worker process did not terminate") from exc

    def cancel_all(self, identifier: str | None) -> None:
        """Called while the coordinator holds the global transition lock."""
        if identifier is not None:
            project_id(identifier)
        with self.database.transaction(write=True) as con:
            if identifier is None:
                con.execute(
                    "UPDATE jobs SET status='cancelled',finished_ns=? "
                    "WHERE status IN ('queued','running')",
                    (time.time_ns(),),
                )
            else:
                con.execute(
                    "UPDATE jobs SET status='cancelled',finished_ns=? "
                    "WHERE project_id=? AND status IN ('queued','running')",
                    (time.time_ns(), identifier),
                )
        with self._guard:
            active = list(self._running.items())
        for job_id, (thread, proc) in active:
            if proc is None:
                continue  # queued worker rechecks its cancelled state on admission
            if identifier is not None:
                with self.database.transaction() as con:
                    row = con.execute(
                        "SELECT project_id FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                if row is None or row["project_id"] != identifier:
                    continue
            self._terminate(proc)
            thread.join(timeout=8)
            if thread.is_alive():
                raise JobError("Worker thread did not stop")

    def get(self, job_id: str) -> dict:
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT id,project_id,kind,status,action,started_ns,"
                "finished_ns,exit_code FROM jobs WHERE id=?", (job_id,),
            ).fetchone()
        if row is None:
            raise JobError("Unknown job")
        return dict(row)
