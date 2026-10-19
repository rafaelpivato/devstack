"""Bounded command supervision within the controller's process/cgroup.

Web/API clients request only allowlisted saved tasks or Compose actions.
No raw argv, Docker socket or host shell is available over controller IPC.
"""
import json
import os
from pathlib import Path
import stat
import signal
import subprocess
import threading
import time
from uuid import uuid4

from devstack.domain.models import project_id
from devstack.projects.manifest import _tasks, ManifestError
from .coordinator import ControlError

_ALLOWED_COMPOSE = {"build", "restart"}
_LOG_LIMIT = 2 * 1024 * 1024
_LOG_CHUNK = 16 * 1024


class JobError(RuntimeError):
    pass


class JobInvalid(ValueError):
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

    def _task(self, identifier: str, task_name: str, generation: str,
              job_id: str) -> list[str]:
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
        return self.backend.task_command(identifier, data, generation, job_id)

    def submit_task(self, identifier: str, task_name: str) -> str:
        return self._submit(identifier, "task", task_name)

    def submit_compose(self, identifier: str, action: str) -> str:
        if action not in _ALLOWED_COMPOSE:
            raise JobError("Unsupported Compose action")
        return self._submit(identifier, "compose", action)

    @staticmethod
    def _adhoc_spec(kind: str, service: str, image: str,
                    network: str, command: list) -> dict:
        if kind not in ("exec", "run", "image"):
            raise JobInvalid("Invalid ad-hoc execution mode")
        if not all(isinstance(value, str) for value in (service, image, network)):
            raise JobInvalid("Invalid container target")
        if kind == "image" and (not image or service):
            raise JobInvalid("Standalone image requires only an image name")
        if kind != "image" and (image or not service or network != "none"):
            raise JobInvalid("Compose command requires a service and no network override")
        if network not in ("none", "project"):
            raise JobInvalid("Invalid network mode")
        if not isinstance(command, list):
            raise JobInvalid("Command must be an argv array")
        # Delegate validation to the same strict manifest specification used
        # for saved tasks; never allow a caller-defined host/Docker argv.
        try:
            toml = (
                "[[tasks]]\nname = \"adhoc\"\n"
                + "service = " + json.dumps(service) + "\n"
                + "image = " + json.dumps(image) + "\n"
                + "mode = " + json.dumps("run" if kind == "image" else kind) + "\n"
                + "network = " + json.dumps(network) + "\n"
                + "command = " + json.dumps(command) + "\n"
            ).encode("utf-8")
            item = _tasks(toml)[0]
        except (ManifestError, TypeError, ValueError, UnicodeError) as exc:
            raise JobInvalid("Invalid or oversized container command") from exc
        return {"service": item.service, "image": item.image,
                "mode": item.mode, "network": item.network,
                "argv": list(item.argv)}

    def submit_adhoc(self, identifier: str, kind: str, service: str,
                     image: str, network: str, command: list) -> str:
        spec = self._adhoc_spec(kind, service, image, network, command)
        return self._submit(identifier, "adhoc",
                            kind + ": " + (service or image), adhoc=spec)

    def _submit(self, identifier: str, kind: str, action: str,
                adhoc: dict | None = None) -> str:
        project_id(identifier)
        with self.coordinator.locked():
            state = self.coordinator.status()
            self.coordinator.admit(identifier, state["generation"] or "")
            job_id = str(uuid4())
            if kind == "task":
                argv = self._task(identifier, action, state["generation"], job_id)
            elif kind == "adhoc":
                argv = self.backend.task_command(
                    identifier, adhoc, state["generation"], job_id,
                )
            else:
                argv = self.backend.command(identifier) + [action]
            if not all(isinstance(arg, str) and arg for arg in argv):
                raise JobError("Invalid worker command")
            with self.database.transaction() as con:
                existing = con.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()
            if existing:
                raise JobError("A project command is already in progress")
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
                with log_path.open("xb"):
                    pass
                log_path.chmod(0o600)
                proc = subprocess.Popen(
                    list(argv), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                # Stream to a bounded private log without blocking Docker
                # when its stdout is larger than our retention allowance.
                drain = threading.Thread(
                    target=self._drain, args=(proc, log_path),
                    daemon=True, name="devstack-job-log-" + job_id[:8],
                )
                drain.start()
                with self._guard:
                    self._running[job_id] = (threading.current_thread(), proc)
                self._set(job_id, "running")
            try:
                exit_code = proc.wait(timeout=900)
                final = "succeeded" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired:
                self._terminate(proc)
                final, exit_code = "failed", None
            drain.join(timeout=10)
            if drain.is_alive():
                raise JobError("Worker log stream did not finish")
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
    def _drain(proc: subprocess.Popen, log_path: Path) -> None:
        written = 0
        with log_path.open("wb", buffering=0) as output:
            while True:
                data = proc.stdout.read(4096)
                if not data:
                    break
                if written < _LOG_LIMIT:
                    chunk = data[:_LOG_LIMIT - written]
                    output.write(chunk)
                    written += len(chunk)
        proc.stdout.close()

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

    def cancel(self, identifier: str, job_id: str) -> dict:
        project_id(identifier)
        project_id(job_id)
        with self.coordinator.locked():
            current = self.get(job_id)
            if current["project_id"] != identifier:
                raise JobError("Job does not belong to requested project")
            if current["status"] not in ("queued", "running"):
                return current
            state = self.coordinator.status()
            with self.database.transaction() as con:
                row = con.execute("SELECT generation FROM jobs WHERE id=?",
                                  (job_id,)).fetchone()
            if (state["phase"] != "active" or state["project_id"] != identifier
                    or not row or row["generation"] != state["generation"]):
                raise JobError("Cannot cancel work from an inactive or stale generation")
            # Killing Docker CLI alone does NOT stop a Compose exec process
            # running inside a container. Cancellation must drain all workers,
            # stop the owning containers, and verify ownership is globally
            # clear before reporting a job cancelled.
            self.coordinator._stop(state, "deactivating")
            self.coordinator._update("inactive", None)
            return self.get(job_id)

    def list(self, identifier: str) -> dict:
        project_id(identifier)
        self.coordinator.registry.get(identifier)
        with self.database.transaction() as con:
            rows = con.execute(
                "SELECT id,project_id,kind,status,action,started_ns,"
                "finished_ns,exit_code FROM jobs WHERE project_id=? "
                "ORDER BY started_ns DESC,id DESC LIMIT 40",
                (identifier,),
            ).fetchall()
        return {"jobs": [dict(row) for row in rows]}

    def logs(self, identifier: str, job_id: str, offset: int = 0) -> dict:
        project_id(identifier)
        project_id(job_id)
        if type(offset) is not int or not 0 <= offset <= _LOG_LIMIT:
            raise JobInvalid("Invalid job log offset")
        current = self.get(job_id)
        if current["project_id"] != identifier:
            raise JobError("Job does not belong to requested project")
        file = self.workspaces.path(identifier) / "logs" / (job_id + ".log")
        try:
            fd = os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {"text": "", "offset": offset, "next_offset": offset,
                    "complete": current["status"] not in ("queued", "running")}
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _LOG_LIMIT:
                raise JobError("Unsafe or oversized log file")
            data = os.pread(fd, _LOG_CHUNK, offset)
        finally:
            os.close(fd)
        return {"text": data.decode("utf-8", errors="replace"),
                "offset": offset, "next_offset": offset + len(data),
                "complete": current["status"] not in ("queued", "running")}

    def get(self, job_id: str) -> dict:
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT id,project_id,kind,status,action,started_ns,"
                "finished_ns,exit_code FROM jobs WHERE id=?", (job_id,),
            ).fetchone()
        if row is None:
            raise JobError("Unknown job")
        return dict(row)
