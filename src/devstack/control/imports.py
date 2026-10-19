"""Durable, idempotent background GitHub imports owned by the control daemon.

No browser or HTTP request owns the lifetime of git clone. Restart never
replays a clone implicitly; it reconciles committed results and marks
interrupted operations failed, requiring an explicit new request.
"""
import hashlib
import json
import re
import sqlite3
import threading
import time
from uuid import uuid4

from devstack.adapters.github import parse_github_url
from devstack.domain.models import display_name, slug

_KEY = re.compile(r"[0-9a-f]{32}\Z")


class ImportConflict(RuntimeError):
    pass


class ImportNotFound(LookupError):
    pass


class ImportSupervisor:
    def __init__(self, database, registry, provisioner):
        self.database = database
        self.registry = registry
        self.provisioner = provisioner
        self._guard = threading.Lock()
        self._threads = {}

    @staticmethod
    def _digest(url: str, project_slug: str, name: str) -> str:
        payload = json.dumps([url, project_slug, name], separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, identifier: str) -> dict:
        try:
            if str(__import__("uuid").UUID(identifier)) != identifier:
                raise ValueError()
        except (TypeError, ValueError, AttributeError) as exc:
            raise ImportNotFound("Unknown import operation") from exc
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT id,slug,name,status,project_id,error_code,started_ns,"
                "finished_ns FROM import_operations WHERE id=?", (identifier,),
            ).fetchone()
        if row is None:
            raise ImportNotFound("Unknown import operation")
        return dict(row)

    def submit(self, url: str, project_slug: str, name: str, request_key: str) -> dict:
        # Validate before reserving the operation. Do not accept GitHub
        # credentials or untrusted hostnames as repository sources.
        parse_github_url(url)
        slug(project_slug)
        display_name(name)
        if not isinstance(request_key, str) or not _KEY.fullmatch(request_key):
            raise ImportConflict("A 32-character hexadecimal idempotency key is required")
        digest = self._digest(url, project_slug, name)
        with self._guard:
            with self.database.transaction(write=True) as con:
                old = con.execute(
                    "SELECT id,request_digest FROM import_operations WHERE request_key=?",
                    (request_key,),
                ).fetchone()
                if old:
                    if old["request_digest"] != digest:
                        raise ImportConflict("Idempotency key used for another import")
                    identifier = old["id"]
                    create = False
                else:
                    busy = con.execute(
                        "SELECT 1 FROM import_operations WHERE status IN ('queued','running')"
                    ).fetchone()
                    if busy:
                        raise ImportConflict("Another GitHub import is in progress")
                    identifier = str(uuid4())
                    target = str(uuid4())
                    con.execute(
                        "INSERT INTO import_operations"
                        "(id,request_key,request_digest,url,slug,name,target_id,status,started_ns)"
                        " VALUES(?,?,?,?,?,?,?,'queued',?)",
                        (identifier, request_key, digest, url, project_slug, name,
                         target, time.time_ns()),
                    )
                    create = True
            if create:
                thread = threading.Thread(
                    target=self._work, args=(identifier,),
                    name="devstack-import-" + identifier[:8], daemon=True,
                )
                self._threads[identifier] = thread
                try:
                    thread.start()
                except RuntimeError:
                    self._finish(identifier, "failed", None, "ImportWorkerUnavailable")
                    raise ImportConflict("Could not start background import")
        return self.get(identifier)

    def _finish(self, identifier: str, status: str,
                project_id: str | None, code: str = "") -> None:
        with self.database.transaction(write=True) as con:
            con.execute(
                "UPDATE import_operations SET status=?,project_id=?,error_code=?,"
                "finished_ns=? WHERE id=?",
                (status, project_id, code, time.time_ns(), identifier),
            )

    def _work(self, identifier: str) -> None:
        try:
            with self.database.transaction(write=True) as con:
                changed = con.execute(
                    "UPDATE import_operations SET status='running' "
                    "WHERE id=? AND status='queued'", (identifier,),
                ).rowcount
                if changed != 1:
                    return
                row = con.execute(
                    "SELECT url,slug,name,target_id FROM import_operations WHERE id=?",
                    (identifier,),
                ).fetchone()
                url, project_slug, name, target = (row["url"], row["slug"],
                                                   row["name"], row["target_id"])
            try:
                entry = self.provisioner.import_github(
                    url, project_slug, name, identifier=target,
                )
            except Exception as exc:
                # Never persist arbitrary command output, repository URLs,
                # exception descriptions or credential material in public status.
                code = type(exc).__name__
                if code not in {
                    "ProjectConflict", "DomainError", "GitHubSourceError",
                    "ManifestError", "ProjectProvisionError", "WorkspaceError",
                }:
                    code = "ImportFailed"
                self._finish(identifier, "failed", None, code)
            else:
                self._finish(identifier, "succeeded", entry.id)
        finally:
            with self._guard:
                self._threads.pop(identifier, None)

    def reconcile(self) -> tuple[str, ...]:
        """Startup only: recover published projects, never restart old clones.

        An operation can have committed a project immediately before a crash,
        so verify both identity and GitHub source before marking it succeeded.
        """
        self.provisioner.recover_published_imports()
        recovered = []
        with self.database.transaction(write=True) as con:
            rows = con.execute(
                "SELECT id,target_id,url,slug FROM import_operations "
                "WHERE status IN ('queued','running')",
            ).fetchall()
            for row in rows:
                location = parse_github_url(row["url"])
                project = con.execute(
                    "SELECT p.id FROM projects AS p "
                    "JOIN project_sources AS ps ON ps.project_id=p.id "
                    "JOIN repositories AS r ON r.id=ps.repository_id "
                    "WHERE p.id=? AND p.slug=? AND p.status='ready' AND r.clone_url=?",
                    (row["target_id"], row["slug"], location.clone_url),
                ).fetchone()
                if project:
                    con.execute(
                        "UPDATE import_operations SET status='succeeded',"
                        "project_id=?,finished_ns=? WHERE id=?",
                        (project["id"], time.time_ns(), row["id"]),
                    )
                else:
                    con.execute(
                        "UPDATE import_operations SET status='failed',"
                        "error_code='InterruptedImport',finished_ns=? WHERE id=?",
                        (time.time_ns(), row["id"]),
                    )
                recovered.append(row["id"])
        return tuple(recovered)
