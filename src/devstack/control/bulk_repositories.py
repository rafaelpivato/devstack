"""Durable background fast-forward across all repositories of one project.

A run owns the global source-write lock throughout the sequence, so no
project activates or branch/source operation interleaves between repositories.
Partial failures are reported per repository, never reset or force-merged.
"""
import json
import re
import threading
import time
from uuid import uuid4

from devstack.domain.models import project_id
from devstack.projects.repositories import RepositoryGitError

_KEY = re.compile(r"[a-f0-9]{32}\Z")
_FINAL = {"succeeded", "partial", "blocked", "interrupted"}


class BulkRepositoryConflict(RuntimeError):
    pass


class BulkRepositoryNotFound(LookupError):
    pass


class BulkRepositoryUpdater:
    def __init__(self, database, registry, coordinator, repositories):
        self.database = database
        self.registry = registry
        self.coordinator = coordinator
        self.repositories = repositories
        self._guard = threading.Lock()
        self._threads = {}

    def get(self, identifier: str, operation_id: str) -> dict:
        project_id(identifier)
        project_id(operation_id)
        self.registry.get(identifier)
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT id,project_id,status,started_ns,finished_ns,items_json "
                "FROM repository_bulk_updates WHERE id=? AND project_id=?",
                (operation_id, identifier),
            ).fetchone()
        if row is None:
            raise BulkRepositoryNotFound("Repository update not found")
        return {
            "id": row["id"], "project_id": row["project_id"],
            "status": row["status"], "started_ns": row["started_ns"],
            "finished_ns": row["finished_ns"], "items": json.loads(row["items_json"]),
        }

    def submit(self, identifier: str, request_key: str) -> dict:
        project_id(identifier)
        if not isinstance(request_key, str) or not _KEY.fullmatch(request_key):
            raise BulkRepositoryConflict("A hexadecimal idempotency key is required")
        with self._guard:
            with self.coordinator.locked():
                self.registry.get(identifier)
                with self.database.transaction() as con:
                    prior = con.execute(
                        "SELECT id FROM repository_bulk_updates "
                        "WHERE project_id=? AND request_key=?",
                        (identifier, request_key),
                    ).fetchone()
                if prior:
                    return self.get(identifier, prior["id"])
                state = self.coordinator.status()
                if state["phase"] != "inactive" or state["project_id"] is not None:
                    raise BulkRepositoryConflict("Deactivate before updating project repositories")
                self.coordinator._verify_clear()
                repositories = self.registry.repositories(identifier)
                if not repositories:
                    raise BulkRepositoryConflict("Project has no repositories")
                with self.database.transaction(write=True) as con:
                    pending = con.execute(
                        "SELECT 1 FROM repository_bulk_updates "
                        "WHERE status IN ('queued','running') LIMIT 1",
                    ).fetchone()
                    if pending:
                        raise BulkRepositoryConflict("Another repository batch is in progress")
                    operation_id = str(uuid4())
                    items = [
                        {"repository_id": repo.id, "name": repo.name,
                         "status": "queued", "reason": None}
                        for repo in repositories
                    ]
                    con.execute(
                        "INSERT INTO repository_bulk_updates "
                        "(id,project_id,request_key,status,items_json,started_ns) "
                        "VALUES(?,?,?,'queued',?,?)",
                        (operation_id, identifier, request_key,
                         json.dumps(items, separators=(",", ":")), time.time_ns()),
                    )
            worker = threading.Thread(
                target=self._work, args=(identifier, operation_id),
                name="devstack-git-bulk-" + operation_id[:8], daemon=True,
            )
            self._threads[operation_id] = worker
            try:
                worker.start()
            except RuntimeError as exc:
                self._threads.pop(operation_id, None)
                self._finish(operation_id, "interrupted")
                raise BulkRepositoryConflict("Could not launch repository update") from exc
        return self.get(identifier, operation_id)

    def _snapshot(self, operation_id: str) -> list[dict]:
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT items_json FROM repository_bulk_updates WHERE id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise BulkRepositoryNotFound("Repository update not found")
        return json.loads(row["items_json"])

    def _save(self, operation_id: str, items: list[dict],
              status: str = "running") -> None:
        with self.database.transaction(write=True) as con:
            con.execute(
                "UPDATE repository_bulk_updates SET status=?,items_json=? "
                "WHERE id=? AND status IN ('queued','running')",
                (status, json.dumps(items, separators=(",", ":")), operation_id),
            )

    def _finish(self, operation_id: str, status: str) -> None:
        with self.database.transaction(write=True) as con:
            con.execute(
                "UPDATE repository_bulk_updates SET status=?,finished_ns=? "
                "WHERE id=? AND status IN ('queued','running')",
                (status, time.time_ns(), operation_id),
            )

    def _update_item(self, identifier: str, item: dict) -> dict:
        result = dict(item)
        try:
            before = self.repositories.status(identifier, item["repository_id"])
            result["branch"] = before["branch"]
            result["previous_head"] = before["head"]
            if before["dirty"]:
                result.update(status="skipped", reason="dirty_worktree")
                return result
            if not before["branch"]:
                result.update(status="skipped", reason="detached_head")
                return result
            if not before["upstream"] or not before["upstream"].startswith("origin/"):
                result.update(status="skipped", reason="no_origin_upstream")
                return result
            after = self.repositories.update_locked(
                identifier, item["repository_id"], before["head"],
            )
            result.update(
                status="updated" if after["head"] != before["head"] else "unchanged",
                reason=None, head=after["head"], branch=after["branch"],
            )
        except RepositoryGitError as exc:
            # Only allowlisted status reasons, never Git stderr, remote URLs,
            # filesystem paths, or credentials.
            msg = str(exc)
            result.update(
                status="skipped" if "Diverged history" in msg else "failed",
                reason=("diverged" if "Diverged history" in msg
                        else "changed_during_update" if "changed" in msg.lower()
                        else "git_error"),
            )
        except Exception:
            result.update(status="failed", reason="inspection_failed")
        return result

    def _work(self, identifier: str, operation_id: str) -> None:
        try:
            with self.coordinator.locked():
                state = self.coordinator.status()
                if state["phase"] != "inactive" or state["project_id"] is not None:
                    self._finish(operation_id, "blocked")
                    return
                self.coordinator._verify_clear()
                items = self._snapshot(operation_id)
                self._save(operation_id, items)
                for index, item in enumerate(items):
                    items[index] = {**item, "status": "running"}
                    self._save(operation_id, items)
                    items[index] = self._update_item(identifier, item)
                    self._save(operation_id, items)
                completed = all(item["status"] in ("updated", "unchanged")
                                for item in items)
                self._finish(operation_id, "succeeded" if completed else "partial")
        except Exception:
            # A restart/crash or lost Docker safety check must never retry Git
            # automatically; retain successful repos and mark unfinished.
            self._finish(operation_id, "interrupted")
        finally:
            with self._guard:
                self._threads.pop(operation_id, None)

    def recover(self) -> int:
        """After daemon restart, surface interrupted state without rerunning Git."""
        with self._guard:
            if any(thread.is_alive() for thread in self._threads.values()):
                raise BulkRepositoryConflict("Cannot recover while repository updates run")
            with self.database.transaction(write=True) as con:
                rows = con.execute(
                    "SELECT id,items_json FROM repository_bulk_updates "
                    "WHERE status IN ('queued','running')",
                ).fetchall()
                for row in rows:
                    items = json.loads(row["items_json"])
                    for item in items:
                        if item["status"] in ("queued", "running"):
                            item["status"] = "interrupted"
                            item["reason"] = "controller_restart"
                    con.execute(
                        "UPDATE repository_bulk_updates "
                        "SET status='interrupted',items_json=?,finished_ns=? "
                        "WHERE id=?",
                        (json.dumps(items, separators=(",", ":")),
                         time.time_ns(), row["id"]),
                    )
            return len(rows)
