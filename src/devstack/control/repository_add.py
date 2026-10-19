"""Durable, idempotent additions of Git repositories to existing projects.

Unpublished checkouts are staged at controller-generated UUID paths. A
published checkout is registered only through the controller's SQLite
transaction. Startup reconciles a crash between rename and database commit;
it never deletes project source or runs Git automatically on restart.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from uuid import uuid4

from devstack.adapters.github import GitHubSource, parse_github_url
from devstack.domain.models import project_id, repo_name

_KEY = re.compile(r"[0-9a-f]{32}\Z")


class RepositoryAddConflict(RuntimeError):
    pass


class RepositoryAddNotFound(LookupError):
    pass


class RepositoryAddSupervisor:
    def __init__(self, database, registry, workspaces, coordinator, github=None):
        self.database = database
        self.registry = registry
        self.workspaces = workspaces
        self.coordinator = coordinator
        self.github = github or GitHubSource()
        self._guard = threading.Lock()
        self._threads = {}

    def _repos_dir(self, identifier: str) -> Path:
        root = self.workspaces.path(identifier)
        folder = root / "repos"
        if not folder.is_dir() or folder.is_symlink():
            raise RepositoryAddConflict("Repository workspace is not available")
        return folder

    def _paths(self, identifier: str, name: str, operation: str):
        folder = self._repos_dir(identifier)
        return folder / (".repo-add-" + operation), folder / name

    def get(self, identifier: str, operation: str) -> dict:
        project_id(identifier)
        project_id(operation)
        self.registry.get(identifier)
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT id,project_id,repo_id,name,status,error_code,started_ns,"
                "finished_ns FROM repository_add_operations WHERE id=? AND project_id=?",
                (operation, identifier),
            ).fetchone()
        if row is None:
            raise RepositoryAddNotFound("Repository operation not found")
        return dict(row)

    def submit(self, identifier: str, url: str, name: str,
               branch: str, request_key: str) -> dict:
        project_id(identifier)
        repo_name(name)
        location = parse_github_url(url)
        if location.tree:
            raise RepositoryAddConflict("Select a repository URL, not a GitHub tree")
        # Clone adapter also validates branch before execution, but do so before
        # reserving the durable operation.
        if (not isinstance(branch, str) or len(branch) > 240
                or (branch and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", branch)
                                or ".." in branch or "//" in branch
                                or branch.endswith(("/", ".", ".lock"))))):
            raise RepositoryAddConflict("Invalid Git branch")
        if not isinstance(request_key, str) or not _KEY.fullmatch(request_key):
            raise RepositoryAddConflict("A hexadecimal idempotency key is required")
        digest = hashlib.sha256(json.dumps(
            [identifier, location.clone_url, name, branch],
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

        with self._guard:
            with self.coordinator.locked():
                self.registry.get(identifier)
                state = self.coordinator.status()
                if state["phase"] != "inactive":
                    raise RepositoryAddConflict("Deactivate the runtime before cloning")
                self.coordinator._verify_clear()
                with self.database.transaction(write=True) as con:
                    old = con.execute(
                        "SELECT id,request_digest FROM repository_add_operations "
                        "WHERE project_id=? AND request_key=?",
                        (identifier, request_key),
                    ).fetchone()
                    if old:
                        if old["request_digest"] != digest:
                            raise RepositoryAddConflict("Idempotency key reused for different arguments")
                        operation = old["id"]
                        create = False
                    else:
                        if con.execute(
                            "SELECT 1 FROM repositories WHERE project_id=? AND name=?",
                            (identifier, name),
                        ).fetchone():
                            raise RepositoryAddConflict("Repository already registered")
                        if con.execute(
                            "SELECT 1 FROM repository_add_operations "
                            "WHERE project_id=? AND name=? "
                            "AND status IN ('queued','running','recovery')",
                            (identifier, name),
                        ).fetchone():
                            raise RepositoryAddConflict("Repository add is already in progress")
                        operation = str(uuid4())
                        con.execute(
                            "INSERT INTO repository_add_operations"
                            "(id,project_id,request_key,request_digest,repo_id,name,"
                            "clone_url,branch,status,started_ns)"
                            " VALUES(?,?,?,?,?,?,?,?,'queued',?)",
                            (operation, identifier, request_key, digest, str(uuid4()),
                             name, location.clone_url, branch, time.time_ns()),
                        )
                        create = True
            if create:
                thread = threading.Thread(
                    target=self._work, args=(identifier, operation),
                    name="devstack-repo-add-" + operation[:8], daemon=True,
                )
                self._threads[operation] = thread
                try:
                    thread.start()
                except RuntimeError as exc:
                    self._finish(identifier, operation, "failed", "WorkerUnavailable")
                    self._threads.pop(operation, None)
                    raise RepositoryAddConflict("Cannot launch repository import") from exc
        return self.get(identifier, operation)

    def _finish(self, identifier: str, operation: str, state: str,
                code: str = "") -> None:
        with self.database.transaction(write=True) as con:
            con.execute(
                "UPDATE repository_add_operations SET status=?,error_code=?,"
                "finished_ns=? WHERE id=? AND project_id=?",
                (state, code, time.time_ns() if state in ("failed", "succeeded")
                 else None, operation, identifier),
            )

    def _verify_checkout(self, checkout: Path, expected_remote: str) -> None:
        gitdir = checkout / ".git"
        if (checkout.is_symlink() or not checkout.is_dir()
                or gitdir.is_symlink() or not gitdir.is_dir()):
            raise RepositoryAddConflict("Cloned checkout is missing or unsafe")
        from devstack.projects.repositories import RepositoryGitService
        remote = RepositoryGitService._git(checkout, "remote", "get-url", "origin")
        if remote != expected_remote:
            raise RepositoryAddConflict("Unexpected Git remote after cloning")
        head = RepositoryGitService._git(checkout, "rev-parse", "--verify", "HEAD")
        if not re.fullmatch(r"[a-f0-9]{40,64}", head or ""):
            raise RepositoryAddConflict("Repository has no valid checked-out HEAD")

    def _work(self, identifier: str, operation: str) -> None:
        published = False
        try:
            with self.database.transaction(write=True) as con:
                changed = con.execute(
                    "UPDATE repository_add_operations SET status='running' "
                    "WHERE id=? AND project_id=? AND status='queued'",
                    (operation, identifier),
                ).rowcount
                if changed != 1:
                    return
                row = con.execute(
                    "SELECT name,clone_url,branch,repo_id FROM repository_add_operations "
                    "WHERE id=? AND project_id=?", (operation, identifier),
                ).fetchone()
            stage, final = self._paths(identifier, row["name"], operation)
            if stage.exists() or stage.is_symlink() or final.exists() or final.is_symlink():
                raise RepositoryAddConflict("Destination is already present")
            self.github.clone_repository(row["clone_url"], stage, row["branch"])
            self._verify_checkout(stage, row["clone_url"])

            # No held transition lock during network clone. Publishing is
            # serialized with activation and source configuration writes.
            with self.coordinator.locked():
                status = self.coordinator.status()
                if status["phase"] != "inactive" or status["project_id"] is not None:
                    raise RepositoryAddConflict("Runtime changed before clone publication")
                self.coordinator._verify_clear()
                if final.exists() or final.is_symlink():
                    raise RepositoryAddConflict("Repository destination is already present")
                os.rename(stage, final)
                published = True
                directory = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                self._verify_checkout(final, row["clone_url"])
                with self.database.transaction(write=True) as con:
                    con.execute(
                        "INSERT INTO repositories"
                        "(id,project_id,name,relative_path,clone_url,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (row["repo_id"], identifier, row["name"],
                         "repos/" + row["name"], row["clone_url"], time.time_ns()),
                    )
                    con.execute(
                        "UPDATE repository_add_operations "
                        "SET status='succeeded',finished_ns=? "
                        "WHERE id=? AND project_id=? AND status='running'",
                        (time.time_ns(), operation, identifier),
                    )
        except Exception as exc:
            # A rename can reach disk and *then* report an error (or a process
            # can crash before updating the in-memory published flag). Trust
            # the final path and durable journal, never a Python local flag.
            stage, final = self._paths(identifier, row["name"], operation)
            if published or final.exists() or final.is_symlink():
                self._finish(identifier, operation, "recovery", "RecoveryRequired")
            else:
                # An unregistered staged checkout is preserved for inspection.
                self._finish(identifier, operation, "failed", type(exc).__name__
                             if type(exc).__name__ in {
                                 "RepositoryAddConflict", "GitHubSourceError",
                                 "WorkspaceError", "ProjectNotFound",
                             } else "RepositoryAddFailed")
        finally:
            with self._guard:
                self._threads.pop(operation, None)

    def reconcile(self) -> tuple[str, ...]:
        """Resolve interrupted operations without resuming network clones.

        If a checkout was already published, verify its identity and register
        it in the same SQLite transaction that marks its operation successful.
        Missing checkouts become failed; hidden staging is never deleted.
        """
        recovered = []
        with self._guard:
            if any(thread.is_alive() for thread in self._threads.values()):
                raise RepositoryAddConflict("Cannot reconcile while clone workers run")
            with self.coordinator.locked():
                state = self.coordinator.status()
                if state["phase"] != "inactive" or state["project_id"] is not None:
                    raise RepositoryAddConflict("Deactivate the runtime before repository recovery")
                self.coordinator._verify_clear()
                with self.database.transaction() as con:
                    rows = con.execute(
                        "SELECT id,project_id,name,clone_url,repo_id,status "
                        "FROM repository_add_operations "
                        "WHERE status IN ('queued','running','recovery') "
                        "ORDER BY started_ns,id",
                    ).fetchall()
                for row in rows:
                    stage, final = self._paths(row["project_id"], row["name"], row["id"])
                    if final.is_symlink() or (final.exists() and not final.is_dir()):
                        raise RepositoryAddConflict("Unsafe published repository requires inspection")
                    if final.exists():
                        self._verify_checkout(final, row["clone_url"])
                        with self.database.transaction(write=True) as con:
                            existing = con.execute(
                                "SELECT id,clone_url FROM repositories "
                                "WHERE project_id=? AND name=?",
                                (row["project_id"], row["name"]),
                            ).fetchone()
                            if existing and (existing["id"] != row["repo_id"]
                                             or existing["clone_url"] != row["clone_url"]):
                                raise RepositoryAddConflict("Published repository identity mismatch")
                            if existing is None:
                                con.execute(
                                    "INSERT INTO repositories"
                                    "(id,project_id,name,relative_path,clone_url,created_at)"
                                    " VALUES(?,?,?,?,?,?)",
                                    (row["repo_id"], row["project_id"], row["name"],
                                     "repos/" + row["name"], row["clone_url"], time.time_ns()),
                                )
                            con.execute(
                                "UPDATE repository_add_operations "
                                "SET status='succeeded',finished_ns=?,error_code='' WHERE id=?",
                                (time.time_ns(), row["id"]),
                            )
                    else:
                        self._finish(row["project_id"], row["id"], "failed", "InterruptedClone")
                    recovered.append(row["id"])
        return tuple(recovered)
