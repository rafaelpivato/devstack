"""v0.5.2: durable project-wide origin fast-forward, using real Git."""
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from devstack.api.client import ControllerRejected
from devstack.api.http import create_app
from devstack.control.bulk_repositories import (
    BulkRepositoryConflict, BulkRepositoryNotFound,
    BulkRepositoryUpdater,
)
from devstack.control.daemon import Controller, ProtocolError
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database


def git(*argv):
    result = subprocess.run(
        ["git", *map(str, argv)], capture_output=True, text=True,
        check=True, timeout=15,
    )
    return result.stdout.strip()


class Docker:
    def __init__(self):
        self.containers = []
    def owned(self):
        return list(self.containers)
    def validate(self, identifier):
        return None
    def start(self, identifier, generation):
        self.containers = [{"project": identifier, "generation": generation}]
    def verify(self, identifier, generation):
        assert self.containers[0]["project"] == identifier
    def stop(self, identifier):
        self.containers.clear()


class ProjectBulkUpdatesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = Database(self.root / "db.sqlite3")
        self.docker = Docker()
        self.controller = Controller(
            self.db, WorkspaceStore(self.root / "projects"), self.docker,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "A").id
        self.b = self.controller.provisioner.create_empty("beta", "B").id
        self.sources = {}
        self.a_backend = self._create("backend", self.a)
        self.a_frontend = self._create("frontend", self.a)
        self.b_backend = self._clone("backend", self.b)

        def transport(op, **args):
            try:
                return self.controller.handle({"op": op, **args})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc
        self.browser = create_app({
            "TESTING": True, "SECRET_KEY": "test", "SESSION_COOKIE_SECURE": False,
        }, transport=transport).test_client()
        self.browser.get("/projects/" + self.a)
        with self.browser.session_transaction() as session:
            self.csrf = session["_csrf"]

    def _create(self, name, identifier):
        origin = self.root / (name + ".git")
        source = self.root / (name + "-source")
        git("init", "--bare", origin)
        git("init", source)
        git("-C", source, "config", "user.name", "Test User")
        git("-C", source, "config", "user.email", "test@example.invalid")
        git("-C", source, "checkout", "-b", "main")
        (source / "README").write_text("initial")
        git("-C", source, "add", ".")
        git("-C", source, "commit", "-m", "first")
        git("-C", source, "remote", "add", "origin", origin)
        git("-C", source, "push", "-u", "origin", "main")
        git("--git-dir", origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.sources[name] = source
        return self._clone(name, identifier)

    def _clone(self, name, identifier):
        checkout = self.root / "projects" / identifier / "repos" / name
        git("clone", self.root / (name + ".git"), checkout)
        return self.controller.registry.add_repository(
            identifier, name, "repos/" + name, str(self.root / (name + ".git")),
        )

    def _checkout(self, identifier, name):
        return self.root / "projects" / identifier / "repos" / name

    def _advance(self, name):
        source = self.sources[name]
        file = source / "README"
        file.write_text(file.read_text() + "\nadvance")
        git("-C", source, "commit", "-am", "next")
        git("-C", source, "push", "origin", "main")

    def _wait(self, operation):
        for _ in range(250):
            status = self.controller.bulk_repositories.get(
                operation["project_id"], operation["id"],
            )
            if status["status"] not in ("queued", "running"):
                return status
            time.sleep(.02)
        self.fail("Timed out waiting for bulk source update")

    def _start(self, project=None, key=None):
        return self.controller.bulk_repositories.submit(
            project or self.a, key or uuid4().hex,
        )

    def test_updates_all_and_preserves_other_project(self):
        old_a = self.controller.repositories.status(
            self.a, self.a_backend.id,
        )["head"]
        old_b = self.controller.repositories.status(
            self.b, self.b_backend.id,
        )["head"]
        self._advance("backend")
        self._advance("frontend")
        result = self._wait(self._start())
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual({x["status"] for x in result["items"]}, {"updated"})
        self.assertNotEqual(
            self.controller.repositories.status(self.a, self.a_backend.id)["head"], old_a,
        )
        self.assertEqual(
            self.controller.repositories.status(self.b, self.b_backend.id)["head"], old_b,
        )
        again = self._wait(self._start())
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual({x["status"] for x in again["items"]}, {"unchanged"})
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")

    def test_partial_results_continue_without_overwriting_dirty_repo(self):
        self._advance("backend")
        self._advance("frontend")
        dirty = self._checkout(self.a, "backend") / "important.txt"
        dirty.write_text("preserve untracked work")
        old_backend = self.controller.repositories.status(
            self.a, self.a_backend.id,
        )["head"]
        result = self._wait(self._start())
        self.assertEqual(result["status"], "partial")
        statuses = {x["name"]: x for x in result["items"]}
        self.assertEqual(statuses["backend"]["reason"], "dirty_worktree")
        self.assertEqual(statuses["frontend"]["status"], "updated")
        self.assertEqual(
            self.controller.repositories.status(self.a, self.a_backend.id)["head"],
            old_backend,
        )
        self.assertEqual(dirty.read_text(), "preserve untracked work")

    def test_detached_or_no_origin_upstream_is_skipped(self):
        backend = self._checkout(self.a, "backend")
        frontend = self._checkout(self.a, "frontend")
        git("-C", backend, "checkout", "--detach", "HEAD")
        git("-C", frontend, "branch", "--unset-upstream")
        result = self._wait(self._start())
        self.assertEqual(result["status"], "partial")
        reasons = {x["name"]: x["reason"] for x in result["items"]}
        self.assertEqual(reasons["backend"], "detached_head")
        self.assertEqual(reasons["frontend"], "no_origin_upstream")

    def test_diverged_history_is_skipped_not_rewritten(self):
        self._advance("backend")
        backend = self._checkout(self.a, "backend")
        git("-C", backend, "config", "user.name", "Test User")
        git("-C", backend, "config", "user.email", "test@example.invalid")
        (backend / "local.txt").write_text("local commit")
        git("-C", backend, "add", ".")
        git("-C", backend, "commit", "-m", "local")
        local_head = git("-C", backend, "rev-parse", "HEAD")
        result = self._wait(self._start())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["items"][0]["reason"], "diverged")
        self.assertEqual(git("-C", backend, "rev-parse", "HEAD"), local_head)
        self.assertEqual(result["items"][1]["status"], "unchanged")

    def test_retry_key_is_idempotent_and_cross_project_operation_is_hidden(self):
        key = uuid4().hex
        first = self._start(key=key)
        second = self._start(key=key)
        self.assertEqual(first["id"], second["id"])
        done = self._wait(first)
        self.assertEqual(done["status"], "succeeded")
        with self.assertRaises(BulkRepositoryNotFound):
            self.controller.bulk_repositories.get(self.b, first["id"])
        with self.assertRaises(BulkRepositoryConflict):
            self._start(key="invalid")
        with self.assertRaises(ProtocolError):
            self.controller.handle({
                "op": "repository_bulk_submit", "project_id": self.a,
                "request_key": uuid4().hex, "git_command": "reset --hard",
            })

    def test_running_runtime_rejects_bulk_source_writes(self):
        self.controller.coordinator.activate(self.b)
        with self.assertRaises(BulkRepositoryConflict):
            self._start()
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.b)
        self.controller.coordinator.deactivate()

    def test_active_owner_can_run_batch_without_stopping_services(self):
        self.controller.coordinator.activate(self.a)
        state = self.controller.coordinator.status()
        containers = list(self.docker.containers)
        self._advance("backend")
        receipt = self._wait(self._start())
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(self.controller.coordinator.status(), state)
        self.assertEqual(self.docker.containers, containers)
        self.controller.coordinator.deactivate()

    def test_activation_waits_until_bulk_source_update_finishes(self):
        from threading import Event, Thread
        from unittest.mock import patch

        entered = Event()
        release = Event()
        finished_activation = Event()
        self._advance("backend")
        original = self.controller.repositories.update_locked

        def paused_update(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test update gate timed out")
            return original(*args)

        with patch.object(self.controller.repositories, "update_locked",
                          side_effect=paused_update):
            op = self._start()
            self.assertTrue(entered.wait(3), "Bulk worker did not reach Git")
            def activate():
                try:
                    self.controller.coordinator.activate(self.b)
                finally:
                    finished_activation.set()

            activator = Thread(target=activate, daemon=True)
            activator.start()
            self.assertFalse(
                finished_activation.wait(.15),
                "Another project activated during the source update batch",
            )
            release.set()
            self.assertEqual(self._wait(op)["status"], "succeeded")
            self.assertTrue(finished_activation.wait(4))
            activator.join(timeout=1)
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.b)
        self.controller.coordinator.deactivate()

    def test_blocked_worker_does_not_touch_repositories(self):
        self.controller.coordinator.activate(self.b)
        before = self.controller.repositories.status(self.a, self.a_backend.id)["head"]
        op = str(uuid4())
        items = [
            {"repository_id": self.a_backend.id, "name": "backend",
             "status": "queued", "reason": None},
            {"repository_id": self.a_frontend.id, "name": "frontend",
             "status": "queued", "reason": None},
        ]
        with self.db.transaction(write=True) as con:
            con.execute(
                "INSERT INTO repository_bulk_updates "
                "(id,project_id,request_key,status,items_json,started_ns) "
                "VALUES(?,?,?,'queued',?,?)",
                (op, self.a, uuid4().hex, json.dumps(items), time.time_ns()),
            )
        self.controller.bulk_repositories._work(self.a, op)
        record = self.controller.bulk_repositories.get(self.a, op)
        self.assertEqual(record["status"], "blocked")
        self.assertEqual({x["reason"] for x in record["items"]},
                         {"runtime_active"})
        self.assertEqual(
            self.controller.repositories.status(self.a, self.a_backend.id)["head"],
            before,
        )
        self.controller.coordinator.deactivate()

    def test_restart_marks_interrupted_without_replaying_remote_updates(self):
        items = [
            {"repository_id": self.a_backend.id, "name": "backend",
             "status": "updated", "reason": None},
            {"repository_id": self.a_frontend.id, "name": "frontend",
             "status": "running", "reason": None},
        ]
        op = str(uuid4())
        with self.db.transaction(write=True) as con:
            con.execute(
                "INSERT INTO repository_bulk_updates "
                "(id,project_id,request_key,status,items_json,started_ns) "
                "VALUES(?,?,?,'running',?,?)",
                (op, self.a, uuid4().hex, json.dumps(items), time.time_ns()),
            )
        fresh = BulkRepositoryUpdater(
            self.db, self.controller.registry, self.controller.coordinator,
            self.controller.repositories,
        )
        self.assertEqual(fresh.recover(), 1)
        recovered = fresh.get(self.a, op)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["items"][0]["status"], "updated")
        self.assertEqual(recovered["items"][1]["status"], "interrupted")
        self.assertEqual(fresh.recover(), 0)

    def test_api_and_mobile_progress_read_after_browser_disconnect(self):
        self._advance("backend")
        path = "/api/v2/projects/" + self.a + "/repositories/update-all"
        key = uuid4().hex
        self.assertEqual(self.browser.post(
            path, json={}, headers={"Idempotency-Key": key},
        ).status_code, 403)
        response = self.browser.post(
            path, json={}, headers={
                "Idempotency-Key": key, "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertTrue(response.headers["Location"].endswith(
            "/repository-updates/" + response.get_json()["id"],
        ))
        operation = self._wait(response.get_json())
        self.assertEqual(operation["status"], "succeeded")
        status_url = response.headers["Location"]
        self.assertEqual(self.browser.get(status_url).status_code, 200)
        self.assertEqual(self.browser.get(
            "/projects/" + self.a + "/repositories/updates/" + operation["id"],
        ).status_code, 200)
        dashboard = self.browser.get("/projects/" + self.a)
        self.assertIn(b"devstack-csrf", dashboard.data)
        self.assertEqual(self.browser.get(
            "/api/v2/projects/" + self.b +
            "/repository-updates/" + operation["id"],
        ).status_code, 404)
        self.assertEqual(self.browser.post(
            path, json={"force": True}, headers={
                "Idempotency-Key": uuid4().hex, "X-CSRF-Token": self.csrf,
            },
        ).status_code, 400)


if __name__ == "__main__":
    unittest.main()
