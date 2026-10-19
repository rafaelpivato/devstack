"""Durable GitHub import job guarantees for mobile clients and daemon restarts."""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from devstack.control.imports import ImportConflict, ImportSupervisor
from devstack.projects.service import ProjectProvisioner
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database
from devstack.storage.projects import ProjectRegistry

URL = "https://github.com/rafaelpivato/devstack/tree/main/examples/smoke"


class ImportOperationTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.base = Path(home.name)
        self.db = Database(self.base / "control.sqlite3")
        self.registry = ProjectRegistry(self.db)
        self.workspaces = WorkspaceStore(self.base / "projects")
        self.provisioner = ProjectProvisioner(self.registry, self.workspaces)
        self.imports = ImportSupervisor(self.db, self.registry, self.provisioner)

    def _terminal(self, identifier):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            state = self.imports.get(identifier)
            if state["status"] in ("succeeded", "failed"):
                return state
            time.sleep(.01)
        self.fail("Import job did not settle")

    def test_same_idempotency_key_never_clones_twice(self):
        waiting = threading.Event()
        done = threading.Event()
        def clone(*_args):
            waiting.set()
            done.wait(4)
            return self.registry.create("actual", "Imported")
        self.provisioner.import_github = Mock(side_effect=clone)
        key = uuid4().hex
        try:
            first = self.imports.submit(URL, "smoke", "Smoke", key)
            self.assertTrue(waiting.wait(2))
            again = self.imports.submit(URL, "smoke", "Smoke", key)
            self.assertEqual(again["id"], first["id"])
            self.assertEqual(self.provisioner.import_github.call_count, 1)
            with self.assertRaises(ImportConflict):
                self.imports.submit(URL, "different", "Different", key)
            with self.assertRaises(ImportConflict):
                self.imports.submit(URL, "second", "Second", uuid4().hex)
        finally:
            done.set()
        finished = self._terminal(first["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertIsNotNone(finished["project_id"])
        reopened = ImportSupervisor(Database(self.db.path), self.registry, self.provisioner)
        self.assertEqual(reopened.get(first["id"]), finished)

    def test_failures_are_persistent_but_do_not_leak_exception_messages(self):
        self.provisioner.import_github = Mock(
            side_effect=RuntimeError("private-token should never be shown")
        )
        started = self.imports.submit(URL, "smoke", "Smoke", uuid4().hex)
        state = self._terminal(started["id"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error_code"], "ImportFailed")
        self.assertNotIn("private-token", str(state))
        self.assertFalse(self.registry.list())

    def test_restart_marks_interrupted_operations_failed_without_replaying(self):
        identifier = str(uuid4())
        with self.db.transaction(write=True) as con:
            con.execute(
                "INSERT INTO import_operations"
                "(id,request_key,request_digest,url,slug,name,status,started_ns)"
                " VALUES(?,?,?,?,? ,?,'running',?)",
                (identifier, uuid4().hex, "digest", URL, "smoke", "Smoke",
                 time.time_ns()),
            )
        self.provisioner.import_github = Mock(side_effect=AssertionError("must not rerun"))
        self.provisioner.recover_published_imports = Mock(return_value=())
        restarted = ImportSupervisor(self.db, self.registry, self.provisioner)
        self.assertEqual(restarted.reconcile(), (identifier,))
        status = restarted.get(identifier)
        self.assertEqual((status["status"], status["error_code"]),
                         ("failed", "InterruptedImport"))
        self.provisioner.import_github.assert_not_called()
        self.assertEqual(restarted.reconcile(), ())

    def test_restart_resolves_committed_project_after_crash_before_job_update(self):
        entry = self.registry.create("smoke", "Smoke")
        repo = self.registry.add_repository(
            entry.id, "devstack", "repos/devstack",
            "https://github.com/rafaelpivato/devstack.git",
        )
        with self.db.transaction(write=True) as con:
            con.execute(
                "INSERT INTO project_sources(project_id,repository_id,subdirectory,branch)"
                " VALUES(?,?,?,?)",
                (entry.id, repo.id, "examples/smoke", "main"),
            )
        identifier = str(uuid4())
        with self.db.transaction(write=True) as con:
            con.execute(
                "INSERT INTO import_operations"
                "(id,request_key,request_digest,url,slug,name,status,started_ns)"
                " VALUES(?,?,?,?,?,?,'running',?)",
                (identifier, uuid4().hex, "digest", URL, "smoke", "Smoke",
                 time.time_ns()),
            )
        self.provisioner.recover_published_imports = Mock(return_value=())
        self.assertEqual(self.imports.reconcile(), (identifier,))
        state = self.imports.get(identifier)
        self.assertEqual((state["status"], state["project_id"]),
                         ("succeeded", entry.id))

    def test_invalid_github_or_key_does_not_write_a_job(self):
        for url, key in ((URL, "not a key"), ("https://evil.invalid/o/r", uuid4().hex)):
            with self.assertRaises((ValueError, ImportConflict)):
                self.imports.submit(url, "smoke", "Smoke", key)
        with self.db.transaction() as con:
            count = con.execute("SELECT COUNT(*) FROM import_operations").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
