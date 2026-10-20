"""Single global, crash-reconciling runtime coordinator for all projects.

The coordinator runs exclusively in the systemd control daemon. Every Docker
transition is serialized by an OS file lock, and all activity must carry the
registered project UUID plus its current activation generation.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets
import time

from devstack.domain.models import project_id
from devstack.storage.database import Database
from devstack.storage.projects import ProjectRegistry

_STATES = {"inactive", "activating", "active", "deactivating", "switching", "error"}


class ControlError(RuntimeError):
    """Transition failed or runtime execution is not authorized."""


class Coordinator:
    def __init__(self, database: Database, registry: ProjectRegistry, backend):
        self.database = database
        self.registry = registry
        self.backend = backend
        self.jobs = None
        self.ingress = None
        self.lock_path = database.path.with_suffix(".runtime.lock")
        if self.lock_path.is_symlink():
            raise ControlError("Runtime lock path must not be a symlink")

    @contextmanager
    def locked(self):
        descriptor = os.open(
            self.lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def status(self) -> dict:
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT phase,project_id,generation,message,updated_ns "
                "FROM runtime_state WHERE singleton=1",
            ).fetchone()
        if row is None or row["phase"] not in _STATES:
            raise ControlError("Runtime state is missing or invalid")
        return {
            "phase": row["phase"], "project_id": row["project_id"],
            "generation": row["generation"], "message": row["message"],
            "updated_ns": row["updated_ns"],
        }

    def _update(self, phase: str, identifier: str | None,
                generation: str | None = None, message: str = "") -> dict:
        if phase not in _STATES:
            raise ControlError("Invalid runtime phase")
        if identifier is not None:
            project_id(identifier)
        with self.database.transaction(write=True) as con:
            con.execute(
                "UPDATE runtime_state SET phase=?,project_id=?,generation=?,"
                "message=?,updated_ns=? WHERE singleton=1",
                (phase, identifier, generation, message[:800], time.time_ns()),
            )
        return self.status()

    def _inspect(self) -> list[dict]:
        try:
            return self.backend.owned()
        except Exception as exc:
            raise ControlError("Cannot inspect Docker ownership") from exc

    def _verify_clear(self, *, allow_pending_sync: bool = False) -> None:
        if not allow_pending_sync:
            with self.database.transaction() as con:
                pending = con.execute(
                    "SELECT 1 FROM source_sync_pending LIMIT 1"
                ).fetchone()
                unresolved = con.execute(
                    "SELECT 1 FROM repository_add_operations "
                    "WHERE status='recovery' LIMIT 1"
                ).fetchone()
            if pending is not None or unresolved is not None:
                raise ControlError("Unfinished source or repository publication blocks activation")
        owned = self._inspect()
        if owned:
            raise ControlError("Unverified or remaining DevStack-owned containers prevent activation")

    def _validate(self, identifier: str) -> None:
        self.registry.get(identifier)
        try:
            self.backend.validate(identifier)
        except Exception as exc:
            raise ControlError("Target project Compose configuration is invalid") from exc

    def _activate(self, identifier: str) -> dict:
        # Caller owns global lock, target is validated and globally clear.
        generation = secrets.token_hex(16)
        self._update("activating", identifier, generation)
        try:
            self.backend.start(identifier, generation)
            self.backend.verify(identifier, generation)
            # A stale or unregistered container anywhere blocks ownership.
            owned = self._inspect()
            if any(item["project"] != identifier or
                   item["generation"] != generation for item in owned):
                raise ControlError("Unexpected Docker ownership during activation")
        except Exception as exc:
            try:
                self.backend.stop(identifier)
                self._verify_clear()
                message = "Activation failed; project stopped"
            except Exception:
                message = "Activation failed; Docker cleanup could not be verified"
            self._update("error", identifier, None, message)
            raise ControlError(message) from exc
        return self._update("active", identifier, generation)

    def activate(self, identifier: str) -> dict:
        with self.locked():
            if self.status()["phase"] != "inactive":
                raise ControlError("Runtime is not inactive")
            self._validate(identifier)
            self._verify_clear()
            return self._activate(identifier)

    def _stop(self, current: dict, transition: str) -> None:
        identifier = current["project_id"]
        self._update(transition, identifier, None)
        try:
            if self.ingress is not None:
                self.ingress.drain(identifier)
            if self.jobs is not None:
                self.jobs.cancel_all(identifier)
            self.backend.stop(identifier)
            self._verify_clear()
        except Exception as exc:
            self._update("error", identifier, None,
                         "Deactivation cannot verify that project execution stopped")
            raise ControlError("Runtime deactivation failed closed") from exc

    def deactivate(self) -> dict:
        with self.locked():
            current = self.status()
            if current["phase"] == "inactive":
                self._verify_clear()
                return current
            if current["phase"] != "active":
                raise ControlError("Cannot deactivate an unsettled runtime; reconcile first")
            self._stop(current, "deactivating")
            return self._update("inactive", None)

    def switch(self, target: str) -> dict:
        with self.locked():
            current = self.status()
            if current["phase"] == "inactive":
                self._validate(target)
                self._verify_clear()
                return self._activate(target)
            if current["phase"] != "active":
                raise ControlError("Runtime must be active or inactive before switching")
            if current["project_id"] == target:
                return current
            # Validate before disrupting the current project.
            self._validate(target)
            self._stop(current, "switching")
            self._update("inactive", None)
            # The old project is fully stopped before target is allowed to run.
            return self._activate(target)

    def authorized(self, identifier: str, generation: str) -> bool:
        project_id(identifier)
        current = self.status()
        return (current["phase"] == "active" and
                current["project_id"] == identifier and
                current["generation"] == generation and
                bool(generation))

    def admit(self, identifier: str, generation: str) -> None:
        """Call while holding the global lock immediately before process spawn."""
        if not self.authorized(identifier, generation):
            raise ControlError("Stale or inactive project execution is forbidden")

    def reconcile(self) -> dict:
        """Daemon startup and explicit ERROR recovery; never resumes work."""
        with self.locked():
            previous = self.status()
            self._update("error", previous["project_id"], None,
                         "Checking Docker ownership after controller restart")
            try:
                if self.ingress is not None and previous["project_id"] is not None:
                    self.ingress.drain(previous["project_id"])
                if self.jobs is not None:
                    self.jobs.cancel_all(None)
                registered = {p.id for p in self.registry.list()}
                owned = self._inspect()
                if any(item["project"] not in registered for item in owned):
                    raise ControlError("Unknown DevStack-owned Docker resources")
                for identifier in sorted({item["project"] for item in owned}):
                    self.backend.stop(identifier)
                # Reconciliation must be able to clear stale runtime resources
                # even with a pending source journal. Activation remains blocked
                # until the journal has been safely replayed.
                self._verify_clear(allow_pending_sync=True)
            except Exception as exc:
                self._update("error", previous["project_id"], None,
                             "Runtime recovery blocked: Docker ownership not proven clean")
                raise ControlError("Controller recovery failed closed") from exc
            return self._update("inactive", None)
