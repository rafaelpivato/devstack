"""Issue #90: targeted manual operations and repository-triggered reactions."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack.api.client import ControllerRejected
from devstack.api.http import create_app
from devstack.control.daemon import Controller
from devstack.control.jobs import JobInvalid
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database, SCHEMA_VERSION
from test_v089_repository_sync import git


class Docker:
    def __init__(self):
        self.running = []
        self.calls = []

    def owned(self):
        return list(self.running)

    def validate(self, identifier):
        pass

    def start(self, identifier, generation):
        self.running = [{"project": identifier, "generation": generation}]

    def verify(self, identifier, generation):
        assert all(x["project"] == identifier and
                   x["generation"] == generation for x in self.running)

    def stop(self, identifier):
        self.running = []

    def configured_services(self, identifier):
        return ["api", "worker", "database"]

    def command(self, identifier):
        return ["docker", "compose", "-p", "devstack-" + identifier]


class ReactionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.db = Database(root / "state" / "control.sqlite3")
        self.docker = Docker()
        self.controller = Controller(self.db, WorkspaceStore(root / "projects"),
                                     self.docker)
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "A").id
        self.b = self.controller.provisioner.create_empty("beta", "B").id
        self.origin = root / "origin.git"
        source = root / "source"
        git("init", "--bare", self.origin)
        git("init", source)
        git("-C", source, "config", "user.name", "Tester")
        git("-C", source, "config", "user.email", "test@example.invalid")
        git("-C", source, "checkout", "-b", "main")
        (source / "README").write_text("one")
        git("-C", source, "add", ".")
        git("-C", source, "commit", "-m", "one")
        git("-C", source, "remote", "add", "origin", self.origin)
        git("-C", source, "push", "-u", "origin", "main")
        git("--git-dir", self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.source = source
        self.repo = self._clone(self.a)
        self.foreign_repo = self._clone(self.b)
        self.submissions = []
        def fake_worker(job_id, project_id, generation, argv):
            self.submissions.append((job_id, project_id, generation, list(argv)))
        self.controller.jobs._worker = fake_worker

    def _clone(self, identifier):
        checkout = self.controller.provisioner.workspaces.path(identifier) / "repos" / "code"
        git("clone", self.origin, checkout)
        return self.controller.registry.add_repository(
            identifier, "code", "repos/code", str(self.origin),
        )

    def push(self):
        readme = self.source / "README"
        readme.write_text(readme.read_text() + "\nnext")
        git("-C", self.source, "commit", "-am", "update")
        git("-C", self.source, "push", "origin", "main")

    def wait_job(self, count=1):
        for _ in range(100):
            if len(self.submissions) >= count:
                return self.submissions[-1]
            time.sleep(.01)
        self.fail("Targeted job not dispatched")

    def test_default_none_and_explicit_mapping(self):
        reactions = self.controller.reactions
        rows = reactions.get(self.a, self.repo.id)
        self.assertEqual([s["action"] for s in rows["services"]],
                         ["none", "none", "none"])
        result = reactions.set_policy(self.a, self.repo.id, "api", "redeploy")
        self.assertEqual(result["services"][0]["action"], "redeploy")
        reactions.set_policy(self.a, self.repo.id, "api", "none")
        self.assertEqual(reactions.get(self.a, self.repo.id)["services"][0]["action"], "none")
        with self.assertRaises(Exception):
            reactions.set_policy(self.b, self.repo.id, "api", "restart")
        with self.assertRaises(Exception):
            reactions.set_policy(self.a, self.repo.id, "unknown", "restart")
        with self.assertRaises(Exception):
            reactions.set_policy(self.a, self.repo.id, "api", "shell")

    def test_manual_redeploy_and_restart_are_targeted(self):
        self.controller.coordinator.activate(self.a)
        for index, (action, suffix) in enumerate([
            ("restart", ["restart", "--no-deps", "api"]),
            ("redeploy", ["up", "-d", "--no-deps", "--build",
                          "--force-recreate", "--wait", "--wait-timeout", "150", "api"]),
        ], 1):
            self.controller.jobs.submit_service(self.a, "api", action)
            self.assertEqual(self.wait_job(index)[3][-len(suffix):], suffix)
            # Fake worker does not execute Compose; finalize its queued receipt.
            with self.db.transaction(write=True) as con:
                con.execute("UPDATE jobs SET status='succeeded' "
                            "WHERE status='queued' AND project_id=?", (self.a,))
        with self.assertRaises(JobInvalid):
            self.controller.jobs.submit_service(self.a, "bad --flag", "redeploy")
        with self.assertRaises(Exception):
            self.controller.jobs.submit_service(self.b, "api", "restart")
        self.controller.coordinator.deactivate()

    def test_git_change_schedules_once_but_check_and_unchanged_do_not(self):
        c = self.controller
        c.coordinator.activate(self.a)
        c.reactions.set_policy(self.a, self.repo.id, "api", "redeploy")
        previous = c.repositories.status(self.a, self.repo.id)["head"]
        self.push()
        c.repository_sync.perform(self.a, self.repo.id)  # fetch only
        self.assertEqual(c.reactions.get(self.a, self.repo.id)["events"], [])
        updated = c.handle({"op": "repository_update", "project_id": self.a,
                            "repository_id": self.repo.id, "expected_head": previous})
        self.assertNotEqual(updated["head"], previous)
        job = self.wait_job()
        self.assertEqual(job[1], self.a)
        self.assertEqual(job[3][-1], "api")
        events = c.reactions.get(self.a, self.repo.id)["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["job_id"], job[0])
        self.assertEqual(events[0]["status"], "submitted")
        with self.db.transaction(write=True) as con:
            con.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (job[0],))
        c.handle({"op": "repository_update", "project_id": self.a,
                  "repository_id": self.repo.id, "expected_head": updated["head"]})
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(len(c.reactions.get(self.a, self.repo.id)["events"]), 1)
        c.coordinator.deactivate()

    def test_multiple_repositories_coalesce_to_one_service_job(self):
        c = self.controller
        checkout = c.provisioner.workspaces.path(self.a) / "repos" / "second"
        git("clone", self.origin, checkout)
        second = c.registry.add_repository(self.a, "second", "repos/second",
                                           str(self.origin))
        c.reactions.set_policy(self.a, self.repo.id, "api", "restart")
        c.reactions.set_policy(self.a, second.id, "api", "redeploy")
        c.coordinator.activate(self.a)
        self.push()
        for repo_id in (self.repo.id, second.id):
            before = c.repositories.status(self.a, repo_id)["head"]
            c.repository_sync.perform(self.a, repo_id, update=True, expected_head=before)
        self.assertFalse(self.submissions)
        self.assertTrue(c.reactions.dispatch_pending())
        job = self.wait_job()
        self.assertEqual(job[3][-1], "api")
        self.assertIn("--force-recreate", job[3])
        self.assertEqual(len(self.submissions), 1)
        ids = [c.reactions.get(self.a, repo_id)["events"][0]["job_id"]
               for repo_id in (self.repo.id, second.id)]
        self.assertEqual(ids, [job[0], job[0]])
        c.coordinator.deactivate()

    def test_stale_generation_and_inactive_updates_never_run(self):
        c = self.controller
        c.reactions.set_policy(self.a, self.repo.id, "worker", "restart")
        self.push()
        before = c.repositories.status(self.a, self.repo.id)["head"]
        c.repository_sync.perform(self.a, self.repo.id, update=True, expected_head=before)
        self.assertEqual(c.reactions.get(self.a, self.repo.id)["events"][0]["status"], "skipped")
        self.assertFalse(c.reactions.dispatch_pending())
        c.coordinator.activate(self.a)
        self.push()
        before = c.repositories.status(self.a, self.repo.id)["head"]
        c.repository_sync.perform(self.a, self.repo.id, update=True, expected_head=before)
        c.coordinator.deactivate()
        self.assertFalse(c.reactions.dispatch_pending())
        self.assertEqual(len(self.submissions), 0)
        self.assertEqual(c.reactions.get(self.a, self.repo.id)["events"][0]["reason"],
                         "stale_activation")

    def test_source_update_is_fenced_by_running_compose_job(self):
        c = self.controller
        c.coordinator.activate(self.a)
        c.jobs.submit_service(self.a, "api", "redeploy")
        self.wait_job()
        previous = c.repositories.status(self.a, self.repo.id)["head"]
        self.push()
        with self.assertRaises(Exception):
            c.repository_sync.perform(self.a, self.repo.id, update=True,
                                      expected_head=previous)
        self.assertEqual(c.repositories.status(self.a, self.repo.id)["head"], previous)
        c.coordinator.deactivate()

    def test_policy_http_csrf_and_no_read_side_effects(self):
        c = self.controller
        def transport(op, **kwargs):
            try:
                return c.handle({"op": op, **kwargs})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc
        app = create_app({"TESTING": True, "SECRET_KEY": "test",
                          "SESSION_COOKIE_SECURE": False}, transport=transport)
        client = app.test_client()
        client.get("/projects/" + self.a)
        with client.session_transaction() as session:
            token = session["_csrf"]
        url = f"/api/v2/projects/{self.a}/repositories/{self.repo.id}/reactions"
        self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(self.submissions, [])
        self.assertEqual(client.put(url, json={"service": "api", "action": "restart"}).status_code, 403)
        save = client.put(url, json={"service": "api", "action": "restart"},
                          headers={"X-CSRF-Token": token})
        self.assertEqual(save.status_code, 200)
        self.assertEqual(save.get_json()["services"][0]["action"], "restart")
        self.assertEqual(client.get(
            f"/api/v2/projects/{self.b}/repositories/{self.repo.id}/reactions").status_code, 404)
        svc = f"/api/v2/projects/{self.a}/services/api/actions/redeploy"
        self.assertEqual(client.post(svc).status_code, 403)
        c.coordinator.activate(self.a)
        response = client.post(svc, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 202)
        self.wait_job()
        c.coordinator.deactivate()

    def test_schema_v11_upgrade_preserves_policies_and_projects(self):
        # Simulate v0.6.7 by dropping the new tables before opening the database.
        with self.db.transaction(write=True) as con:
            con.execute("DROP TABLE service_reaction_events")
            con.execute("DROP TABLE service_reaction_policies")
            con.execute("UPDATE schema_version SET version=11 WHERE singleton=1")
        upgraded = Database(self.db.path)
        with upgraded.transaction() as con:
            self.assertEqual(con.execute(
                "SELECT version FROM schema_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(con.execute(
                "SELECT count(*) FROM projects").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
