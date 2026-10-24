"""Issue #89 acceptance: active Git sync, default-off policy and recovery."""
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack.api.client import ControllerRejected
from devstack.api.http import create_app
from devstack.control.daemon import Controller
from devstack.projects.repositories import RepositoryGitError, RepositoryNotFound
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database, SCHEMA_VERSION


def git(*args):
    result = subprocess.run(["git", *map(str, args)], capture_output=True,
                            text=True, timeout=15, check=True)
    return result.stdout.strip()


class Docker:
    def __init__(self):
        self.running = []
        self.starts = 0
        self.stops = 0

    def owned(self):
        return list(self.running)

    def validate(self, identifier):
        return None

    def start(self, identifier, generation):
        self.starts += 1
        self.running = [{"project": identifier, "generation": generation}]

    def verify(self, identifier, generation):
        assert self.running == [{"project": identifier, "generation": generation}]

    def stop(self, identifier):
        self.stops += 1
        self.running.clear()


class RepositorySynchronizationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = Database(self.root / "state" / "control.sqlite3")
        self.docker = Docker()
        self.controller = Controller(self.db, WorkspaceStore(self.root / "projects"),
                                     self.docker)
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "A").id
        self.b = self.controller.provisioner.create_empty("beta", "B").id
        self.origin = self.root / "origin.git"
        self.source = self.root / "source"
        git("init", "--bare", self.origin)
        git("init", self.source)
        git("-C", self.source, "config", "user.name", "Tester")
        git("-C", self.source, "config", "user.email", "test@example.invalid")
        git("-C", self.source, "checkout", "-b", "main")
        (self.source / "README").write_text("one", encoding="utf-8")
        git("-C", self.source, "add", ".")
        git("-C", self.source, "commit", "-m", "initial")
        git("-C", self.source, "remote", "add", "origin", self.origin)
        git("-C", self.source, "push", "-u", "origin", "main")
        git("--git-dir", self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.ar = self.clone(self.a)
        self.br = self.clone(self.b)

    def clone(self, identifier):
        checkout = self.root / "projects" / identifier / "repos" / "code"
        git("clone", self.origin, checkout)
        return self.controller.registry.add_repository(
            identifier, "code", "repos/code", str(self.origin),
        )

    def checkout(self, identifier):
        return self.root / "projects" / identifier / "repos" / "code"

    def advance(self):
        path = self.source / "README"
        path.write_text(path.read_text(encoding="utf-8") + "\nnext", encoding="utf-8")
        git("-C", self.source, "commit", "-am", "advance")
        git("-C", self.source, "push", "origin", "main")

    def status(self, identifier=None, repository_id=None):
        return self.controller.repositories.status(identifier or self.a,
                                                    repository_id or self.ar.id)

    def test_default_manual_and_active_check_then_update_leave_runtime_untouched(self):
        sync = self.controller.repository_sync
        self.assertEqual(sync.state(self.a, self.ar.id)["policy"], "manual")
        self.controller.coordinator.activate(self.a)
        owner = self.controller.coordinator.status()
        running = list(self.docker.running)
        previous = self.status()["head"]
        self.advance()
        checked = sync.perform(self.a, self.ar.id)
        self.assertEqual(checked["head"], previous)
        self.assertEqual(checked["behind"], 1)
        self.assertEqual(checked["sync"]["status"], "checked")
        updated = self.controller.handle({
            "op": "repository_update", "project_id": self.a,
            "repository_id": self.ar.id, "expected_head": previous,
        })
        self.assertNotEqual(updated["head"], previous)
        self.assertEqual(updated["sync"]["status"], "updated")
        self.assertEqual(updated["sync"]["events"][0]["outcome"], "updated")
        self.assertIsNotNone(updated["sync"]["last_updated_ns"])
        self.assertEqual(self.controller.coordinator.status(), owner)
        self.assertEqual(self.docker.running, running)
        self.assertEqual(self.docker.starts, 1)
        self.assertEqual(self.docker.stops, 0)
        self.assertEqual(self.status(self.b, self.br.id)["head"], previous)
        self.controller.coordinator.deactivate()

    def test_auto_check_is_read_only_then_opt_in_updates(self):
        sync = self.controller.repository_sync
        self.controller.coordinator.activate(self.a)
        self.advance()
        previous = self.status()["head"]
        sync.set_policy(self.a, self.ar.id, "check")
        self.assertEqual(sync.poll_once(), 1)
        self.assertEqual(self.status()["head"], previous)
        self.assertEqual(sync.state(self.a, self.ar.id)["behind"], 1)
        self.assertEqual(sync.poll_once(), 0)  # respect 5-minute cadence
        sync.set_policy(self.a, self.ar.id, "update")
        with self.db.transaction(write=True) as con:
            con.execute(
                "UPDATE repository_sync SET last_checked_ns=NULL "
                "WHERE repository_id=?", (self.ar.id,),
            )
        self.assertEqual(sync.poll_once(), 1)
        self.assertNotEqual(self.status()["head"], previous)
        self.assertEqual(self.docker.starts, 1)
        self.assertEqual(self.docker.stops, 0)
        self.controller.coordinator.deactivate()

    def test_dirty_checkout_never_overwritten_and_receipts_are_bounded(self):
        sync = self.controller.repository_sync
        self.advance()
        previous = self.status()["head"]
        (self.checkout(self.a) / "untracked.txt").write_text("private")
        with self.assertRaises(RepositoryGitError):
            sync.perform(self.a, self.ar.id, update=True, expected_head=previous)
        self.assertEqual(self.status()["head"], previous)
        self.assertEqual(sync.state(self.a, self.ar.id)["error_code"], "dirty_worktree")
        checked = sync.perform(self.a, self.ar.id)
        self.assertEqual(checked["sync"]["status"], "blocked")
        self.assertEqual(checked["sync"]["error_code"], "dirty_worktree")
        (self.checkout(self.a) / "untracked.txt").unlink()
        for _ in range(12):
            sync.perform(self.a, self.ar.id)
        self.assertEqual(len(sync.state(self.a, self.ar.id)["events"]), 10)

    def test_diverged_and_missing_upstream_refuse_fast_forward(self):
        sync = self.controller.repository_sync
        checkout = self.checkout(self.a)
        git("-C", checkout, "config", "user.name", "Test User")
        git("-C", checkout, "config", "user.email", "test@example.invalid")
        (checkout / "local.txt").write_text("local")
        git("-C", checkout, "add", "local.txt")
        git("-C", checkout, "commit", "-m", "local")
        self.advance()
        local = self.status()["head"]
        with self.assertRaises(RepositoryGitError):
            sync.perform(self.a, self.ar.id, update=True, expected_head=local)
        self.assertEqual(self.status()["head"], local)
        self.assertEqual(sync.state(self.a, self.ar.id)["error_code"], "diverged")
        git("-C", checkout, "branch", "--unset-upstream")
        with self.assertRaises(RepositoryGitError):
            sync.perform(self.a, self.ar.id, update=True, expected_head=local)
        self.assertEqual(sync.state(self.a, self.ar.id)["error_code"],
                         "no_origin_upstream")

    def test_foreign_active_project_is_not_touched(self):
        sync = self.controller.repository_sync
        sync.set_policy(self.a, self.ar.id, "update")
        self.controller.coordinator.activate(self.b)
        self.advance()
        initial = self.status()["head"]
        with self.assertRaises(RepositoryGitError):
            sync.perform(self.a, self.ar.id, update=True, expected_head=initial)
        self.assertEqual(sync.poll_once(), 0)
        self.assertEqual(self.status()["head"], initial)
        self.assertEqual(self.docker.starts, 1)
        self.assertEqual(self.docker.stops, 0)
        self.controller.coordinator.deactivate()

    def test_interrupted_auto_update_is_not_replayed(self):
        sync = self.controller.repository_sync
        sync.set_policy(self.a, self.ar.id, "update")
        self.advance()
        previous = self.status()["head"]
        sync._begin(self.a, self.ar.id, self.status())
        self.assertEqual(sync.recover(), 1)
        self.assertEqual(sync.state(self.a, self.ar.id)["status"], "interrupted")
        self.assertEqual(sync.poll_once(), 0)
        self.assertEqual(self.status()["head"], previous)
        sync.perform(self.a, self.ar.id)  # deliberate manual check clears interruption
        self.assertEqual(sync.state(self.a, self.ar.id)["status"], "checked")
        self.assertEqual(sync.recover(), 0)

    def test_update_serializes_runtime_switch(self):
        sync = self.controller.repository_sync
        self.controller.coordinator.activate(self.a)
        self.advance()
        entered, release, switched = (threading.Event() for _ in range(3))
        original = self.controller.repositories.update_locked
        outcome = []
        def blocked(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            return original(*args)
        def updater():
            try:
                outcome.append(sync.perform(self.a, self.ar.id, update=True,
                                            expected_head=self.status()["head"]))
            except Exception as exc:
                outcome.append(exc)
        def switcher():
            try:
                self.controller.coordinator.switch(self.b)
            finally:
                switched.set()
        with patch.object(self.controller.repositories, "update_locked",
                          side_effect=blocked):
            t = threading.Thread(target=updater)
            t.start()
            self.assertTrue(entered.wait(3))
            s = threading.Thread(target=switcher)
            s.start()
            self.assertFalse(switched.wait(.15))
            release.set()
            t.join(6)
            self.assertTrue(switched.wait(6))
            s.join(3)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], dict)
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.b)
        self.controller.coordinator.deactivate()

    def test_explicit_ipc_and_http_api_scoping(self):
        def transport(op, **kwargs):
            try:
                return self.controller.handle({"op": op, **kwargs})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc
        app = create_app({"TESTING": True, "SECRET_KEY": "test",
                          "SESSION_COOKIE_SECURE": False}, transport=transport)
        client = app.test_client()
        client.get("/projects/" + self.a)
        with client.session_transaction() as session:
            token = session["_csrf"]
        url = f"/api/v2/projects/{self.a}/repositories/{self.ar.id}"
        self.assertEqual(client.get(url + "/sync").get_json()["policy"], "manual")
        self.assertEqual(client.put(url + "/sync", json={"policy": "update"}).status_code, 403)
        saved = client.put(url + "/sync", json={"policy": "check"},
                           headers={"X-CSRF-Token": token})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["policy"], "check")
        self.assertEqual(client.post(url + "/check", json={},
                                     headers={"X-CSRF-Token": token}).status_code, 200)
        self.assertEqual(client.get(
            f"/api/v2/projects/{self.b}/repositories/{self.ar.id}/sync").status_code, 404)
        self.assertEqual(client.put(url + "/sync", json={"policy": "hack"},
                                    headers={"X-CSRF-Token": token}).status_code, 422)

    def test_schema_v10_migrates_without_resetting_state(self):
        path = self.db.path
        owner = self.controller.coordinator.status()
        with self.db.transaction(write=True) as con:
            con.execute("DROP TABLE repository_sync_events")
            con.execute("DROP TABLE repository_sync")
            con.execute("UPDATE schema_version SET version=10 WHERE singleton=1")
        upgraded = Database(path)
        with upgraded.transaction() as con:
            version = con.execute("SELECT version FROM schema_version WHERE singleton=1").fetchone()[0]
            projects = con.execute("SELECT count(*) FROM projects").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(projects, 2)
        self.assertEqual(self.controller.coordinator.status(), owner)


if __name__ == "__main__":
    unittest.main()
