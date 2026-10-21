"""Durable controller-owned runtime transition receipts.

Only the single privileged controller writes this *sidecar* SQLite journal.
The v0.5.2 control.sqlite3 schema remains untouched, preserving downgrade
compatibility for the already-published release's rollback mechanism.

Operations run on controller-owned threads, not in client IPC handler threads.
No automatic retry/reenactment is permitted after a daemon restart.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from uuid import UUID, uuid4

from devstack.domain.models import project_id

KEY_RE = re.compile(r"[0-9a-f]{32}\Z")
ACTIONS = frozenset({"activate", "switch", "deactivate", "reconcile"})
_SAFE_ERRORS = frozenset({
    "ControlError", "DomainError", "ProjectNotFound", "ProjectConflict",
    "SourceSyncConflict",
})


class TransitionConflict(RuntimeError):
    pass


class TransitionNotFound(LookupError):
    pass


class TransitionSupervisor:
    """Idempotent, one-at-a-time transition submissions via existing Coordinator."""

    def __init__(self, database, coordinator, registry, reconcile):
        self.coordinator = coordinator
        self.registry = registry
        self.reconcile = reconcile
        self.path = database.path.parent / "runtime-transitions.sqlite3"
        if self.path.is_symlink() or (self.path.exists()
                                      and not self.path.is_file()):
            raise TransitionConflict("Unsafe runtime operation journal")
        self._guard = threading.Lock()
        self._threads = {}
        with self._transaction(write=True) as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS transitions (
                     id TEXT PRIMARY KEY,
                     request_key TEXT UNIQUE NOT NULL,
                     action TEXT NOT NULL,
                     project_id TEXT,
                     expected_json TEXT NOT NULL,
                     status TEXT NOT NULL CHECK(status IN
                        ('queued','running','succeeded','failed','interrupted')),
                     result_json TEXT,
                     error_code TEXT NOT NULL DEFAULT '',
                     started_ns INTEGER NOT NULL,
                     finished_ns INTEGER
                   )"""
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS transition_latest "
                "ON transitions(started_ns DESC)"
            )
        self.path.chmod(0o600)

    @contextmanager
    def _transaction(self, *, write=False):
        if self.path.is_symlink():
            raise TransitionConflict("Runtime journal path changed")
        connection = None
        try:
            connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _validate_id(identifier: str) -> str:
        try:
            if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
                raise ValueError()
        except (TypeError, ValueError, AttributeError) as exc:
            raise TransitionNotFound("Unknown transition") from exc
        return identifier

    @staticmethod
    def _visible(row) -> dict:
        result = {key: row[key] for key in (
            "id", "request_key", "action", "project_id", "status",
            "error_code", "started_ns", "finished_ns",
        )}
        result["result"] = (json.loads(row["result_json"])
                            if row["result_json"] is not None else None)
        return result

    def get(self, identifier: str) -> dict:
        self._validate_id(identifier)
        with self._transaction() as con:
            row = con.execute(
                "SELECT id,request_key,action,project_id,status,error_code,"
                "result_json,started_ns,finished_ns FROM transitions WHERE id=?",
                (identifier,),
            ).fetchone()
        if row is None:
            raise TransitionNotFound("Unknown transition")
        return self._visible(row)

    def list(self) -> dict:
        with self._transaction() as con:
            rows = con.execute(
                "SELECT id,request_key,action,project_id,status,error_code,"
                "result_json,started_ns,finished_ns FROM transitions "
                "ORDER BY started_ns DESC,id DESC LIMIT 20",
            ).fetchall()
        return {"operations": [self._visible(row) for row in rows]}

    def submit(self, action: str, target: str | None, key: str) -> dict:
        if action not in ACTIONS:
            raise TransitionConflict("Unrecognized transition action")
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise TransitionConflict("A 32-character hex request key is required")
        if action in {"activate", "switch"}:
            project_id(target)
        elif target is not None:
            raise TransitionConflict("Global transition must not target a project")

        with self._guard:
            # Read the single-flight ledger BEFORE waiting for the coordinator
            # flock: that lock may be held throughout a multi-minute Docker
            # transition. Competing submissions and same-key retries must be
            # fast even while the original operation is running.
            with self._transaction() as con:
                previous = con.execute(
                    "SELECT id,action,project_id FROM transitions WHERE request_key=?",
                    (key,),
                ).fetchone()
                if previous:
                    if previous["action"] != action or previous["project_id"] != target:
                        raise TransitionConflict("Request key reused for another transition")
                    return self.get(previous["id"])
                if con.execute(
                    "SELECT 1 FROM transitions WHERE status IN ('queued','running')"
                ).fetchone():
                    raise TransitionConflict("Another runtime transition is already pending")
            # The single coordinator lock also serializes ordinary web
            # mutations. Snapshot the state under it, then fence *any*
            # intervening web/CLI transition before later execution.
            with self.coordinator.locked():
                with self._transaction(write=True) as con:
                    old = con.execute(
                        "SELECT id,action,project_id FROM transitions WHERE request_key=?",
                        (key,),
                    ).fetchone()
                    if old is not None:
                        if old["action"] != action or old["project_id"] != target:
                            raise TransitionConflict("Request key reused for another transition")
                        identifier = old["id"]
                        create = False
                    else:
                        if con.execute(
                            "SELECT 1 FROM transitions WHERE status IN ('queued','running')"
                        ).fetchone():
                            raise TransitionConflict("Another runtime transition is already pending")
                        if target is not None:
                            self.registry.get(target)
                        status = self.coordinator.status()
                        if action == "activate" and status["phase"] != "inactive":
                            raise TransitionConflict("Runtime must be inactive")
                        if action in {"switch", "deactivate"} and status["phase"] not in (
                            "active", "inactive",
                        ):
                            raise TransitionConflict("Runtime must be settled before transition")
                        identifier = str(uuid4())
                        expected = json.dumps({
                            key: status[key] for key in (
                                "phase", "project_id", "generation", "updated_ns",
                            )
                        }, separators=(",", ":"))
                        con.execute(
                            "INSERT INTO transitions(id,request_key,action,project_id,"
                            "expected_json,status,started_ns) VALUES(?,?,?,?,?,'queued',?)",
                            (identifier, key, action, target, expected, time.time_ns()),
                        )
                        create = True
            if create:
                thread = threading.Thread(
                    target=self._work, args=(identifier,),
                    name="devstack-transition-" + identifier[:8], daemon=True,
                )
                self._threads[identifier] = thread
                try:
                    thread.start()
                except RuntimeError:
                    self._finish(identifier, "failed", error="WorkerUnavailable")
                    self._threads.pop(identifier, None)
                    raise TransitionConflict("Runtime transition worker unavailable")
        return self.get(identifier)

    def _finish(self, identifier: str, status: str, *,
                result: dict | None = None, error: str = "") -> None:
        with self._transaction(write=True) as con:
            con.execute(
                "UPDATE transitions SET status=?,result_json=?,error_code=?,"
                "finished_ns=? WHERE id=? AND status IN ('queued','running')",
                (status, json.dumps(result) if result is not None else None,
                 error, time.time_ns(), identifier),
            )

    def _work(self, identifier: str):
        try:
            with self._transaction(write=True) as con:
                updated = con.execute(
                    "UPDATE transitions SET status='running' "
                    "WHERE id=? AND status='queued'", (identifier,),
                ).rowcount
                if not updated:
                    return
                row = con.execute(
                    "SELECT action,project_id,expected_json FROM transitions WHERE id=?",
                    (identifier,),
                ).fetchone()
            action, target, expected = (row["action"], row["project_id"],
                                        json.loads(row["expected_json"]))
            try:
                if action == "activate":
                    result = self.coordinator.activate(target, expected=expected)
                elif action == "switch":
                    result = self.coordinator.switch(target, expected=expected)
                elif action == "deactivate":
                    result = self.coordinator.deactivate(expected=expected)
                else:
                    result = self.reconcile(expected=expected)
            except Exception as exc:
                name = type(exc).__name__
                self._finish(identifier, "failed",
                             error=name if name in _SAFE_ERRORS else "TransitionFailed")
            else:
                # Never include the activation generation in any public receipt.
                self._finish(identifier, "succeeded",
                             result={k: v for k, v in result.items()
                                     if k != "generation"})
        except BaseException as exc:
            # Worker-internal failures must never leave the receipt permanently
            # looking 'running' while the controller itself is still alive.
            try:
                self._finish(identifier, "failed", error="TransitionFailed")
            except Exception:
                # A broken journal is not silently considered successful.
                pass
            if not isinstance(exc, Exception):
                raise
        finally:
            with self._guard:
                self._threads.pop(identifier, None)

    def recover_interrupted(self) -> int:
        """Called at daemon boot: never automatically rerun an interrupted task.

        Startup reconciles Docker separately and fail-closed. A transition
        that committed just before a crash may remain 'interrupted'; consumers
        must inspect current runtime rather than assume it failed.
        """
        with self._transaction(write=True) as con:
            return con.execute(
                "UPDATE transitions SET status='interrupted',"
                "error_code='InterruptedTransition',finished_ns=? "
                "WHERE status IN ('queued','running')",
                (time.time_ns(),),
            ).rowcount
