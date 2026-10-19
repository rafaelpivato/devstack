"""ADR INV-01/03/09/10: read-only three-way Git template review."""
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from devstack.projects.source_sync import SourceReviewInvalid, SourceSyncConflict

from devstack.adapters.github import GitHubCheckout
from devstack.api.client import ControllerRejected
from devstack.api.http import create_app
from devstack.control.daemon import Controller
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database


URL = "https://github.com/example/repo"


class FakeGitHub:
    def clone_and_resolve(self, url, destination):
        destination.mkdir()
        config = destination / ".devstack"
        config.mkdir()
        (config / "project.toml").write_text(
            '[[applications]]\nname="API"\nservice="api"\nport=8000\n'
        )
        (config / "tasks.toml").write_text(
            '[[tasks]]\nname="tests"\nservice="api"\ncommand=["pytest"]\n'
        )
        (config / "compose.yaml").write_text(
            'services:\n  api:\n    image: python:3.12\n'
        )
        return GitHubCheckout(url, "repo", "main", "", destination)


class FakeBackend:
    def __init__(self):
        self.containers = []
    def owned(self):
        return list(self.containers)
    def validate(self, _identifier):
        pass
    def start(self, identifier, generation):
        self.containers = [{"project": identifier, "generation": generation}]
    def verify(self, _identifier, _generation):
        assert self.containers
    def stop(self, _identifier):
        self.containers = []


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name)
        self.docker = FakeBackend()
        self.controller = Controller(
            Database(self.root / "db" / "control.sqlite3"),
            WorkspaceStore(self.root / "workspaces"), self.docker,
        )
        self.controller.provisioner.github = FakeGitHub()
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.import_github(URL, "alpha", "Alpha")
        self.b = self.controller.provisioner.import_github(URL, "beta", "Beta")
        self.a_source = self.root / "workspaces" / self.a.id / "repos" / "repo" / ".devstack"

    def review(self, identifier):
        return self.controller.handle({"op": "source_review", "project_id": identifier})

    def test_no_diff_until_source_changes(self):
        self.assertEqual(self.review(self.a.id)["changes"], [])
        self.assertEqual(self.review(self.b.id)["changes"], [])

    def test_source_changes_are_project_scoped_and_do_not_apply(self):
        before = self.controller.configuration.settings(self.a.id)
        (self.a_source / "project.toml").write_text(
            '[[applications]]\nname="API"\nservice="api"\nport=9000\n'
            '[[applications]]\nname="Frontend"\nservice="frontend"\nport=5173\n'
        )
        (self.a_source / "tasks.toml").write_text(
            '[[tasks]]\nname="tests"\nservice="api"\ncommand=["pytest", "-q"]\n'
        )
        (self.a_source / "compose.yaml").write_text(
            'services:\n  api:\n    image: python:3.13\n'
        )
        review = self.review(self.a.id)
        self.assertEqual(
            {item["id"] for item in review["changes"]},
            {"application:API", "application:Frontend", "task:tests", "compose.yaml"},
        )
        self.assertTrue(all(item["selectable"] for item in review["changes"]))
        self.assertTrue(all(not item["locally_modified"] for item in review["changes"]))
        self.assertEqual(self.controller.configuration.settings(self.a.id), before)
        self.assertEqual(self.review(self.b.id)["changes"], [])
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")
        self.assertFalse(self.docker.containers)

    def test_customizations_and_upstream_deletions_are_reported_not_applied(self):
        settings = self.controller.configuration.settings(self.a.id)
        settings["applications"][0]["port"] = 8100
        self.controller.configuration.replace_settings(
            self.a.id, settings["applications"], settings["tasks"],
        )
        (self.a_source / "project.toml").write_text(
            '[[applications]]\nname="API"\nservice="api"\nport=9000\n'
        )
        (self.a_source / "tasks.toml").write_text("tasks=[]\n")
        review = self.review(self.a.id)
        changes = {item["id"]: item for item in review["changes"]}
        self.assertTrue(changes["application:API"]["locally_modified"])
        self.assertEqual(changes["task:tests"]["action"], "removed_upstream")
        self.assertFalse(changes["task:tests"]["selectable"])
        self.assertEqual(self.controller.configuration.settings(self.a.id)["tasks"],
                         settings["tasks"])

    def test_source_change_invalidate_revision_and_selected_project_is_inert(self):
        before = self.review(self.a.id)["revision"]
        (self.a_source / "compose.yaml").write_text(
            'services:\n  api:\n    image: python:3.13\n'
        )
        self.assertNotEqual(before, self.review(self.a.id)["revision"])
        self.controller.coordinator.activate(self.b.id)
        snapshot = self.review(self.a.id)
        self.assertEqual(snapshot["changes"][0]["id"], "compose.yaml")
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.b.id)

    def test_v2_read_only_api_and_cross_project_routing(self):
        def transport(op, **fields):
            try:
                return self.controller.handle({"op": op, **fields})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc
        app = create_app({"TESTING": True, "SECRET_KEY": "test",
                          "SESSION_COOKIE_SECURE": False}, transport=transport)
        browser = app.test_client()
        project_url = "/projects/" + self.a.id
        page = browser.get(project_url)
        self.assertIn(b"Review Git template changes", page.data)
        self.assertEqual(browser.get(project_url + "/source-review").status_code, 200)
        api = browser.get("/api/v2/projects/" + self.a.id + "/source-review")
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.get_json()["project_id"], self.a.id)
        self.assertEqual(browser.get(
            "/api/v2/projects/" + str(uuid4()) + "/source-review",
        ).status_code, 404)
        self.assertEqual(browser.post(
            "/api/v2/projects/" + self.a.id + "/source-review",
        ).status_code, 403)


    @staticmethod
    def _lines(*rows):
        return chr(10).join(rows) + chr(10)

    def _change_source(self):
        (self.a_source / "project.toml").write_text(self._lines(
            '[[applications]]', 'name="API"', 'service="api"', 'port=9000',
            '[[applications]]', 'name="Frontend"', 'service="frontend"', 'port=5173',
        ))
        (self.a_source / "tasks.toml").write_text(self._lines(
            '[[tasks]]', 'name="tests"', 'service="api"',
            'command=["pytest", "-q"]',
        ))
        (self.a_source / "compose.yaml").write_text(self._lines(
            'services:', '  api:', '    image: python:3.13',
        ))

    def test_selective_apply_only_selected_definitions_and_baseline(self):
        self._change_source()
        before_b = self.controller.configuration.settings(self.b.id)
        rev = self.review(self.a.id)["revision"]
        result = self.controller.source_review.apply(
            self.a.id, rev, ["application:API"], [],
        )
        self.assertEqual(result["applied"], ["application:API"])
        local = self.controller.configuration.settings(self.a.id)
        self.assertEqual(local["applications"], [{"name": "API", "service": "api", "port": 9000}])
        self.assertEqual(local["tasks"][0]["command"], ["pytest"])
        self.assertEqual(self.controller.configuration.settings(self.b.id), before_b)
        remaining = {item["id"] for item in self.review(self.a.id)["changes"]}
        self.assertEqual(remaining, {"application:Frontend", "task:tests", "compose.yaml"})
        self.assertEqual(self.controller.registry.import_metadata(self.a.id)["repository_id"],
                         self.review(self.a.id)["source_repository_id"])
        with self.assertRaises(SourceSyncConflict):
            self.controller.source_review.apply(self.a.id, rev, ["task:tests"], [])

    def test_conflicting_local_changes_require_specific_overwrite_approval(self):
        data = self.controller.configuration.settings(self.a.id)
        data["applications"][0]["port"] = 8100
        self.controller.configuration.replace_settings(
            self.a.id, data["applications"], data["tasks"],
        )
        (self.a_source / "project.toml").write_text(self._lines(
            '[[applications]]', 'name="API"', 'service="api"', 'port=9000',
        ))
        revision = self.review(self.a.id)["revision"]
        self.assertTrue(self.review(self.a.id)["changes"][0]["locally_modified"])
        for overwrites in ([], ["application:other"]):
            with self.subTest(overwrites=overwrites), self.assertRaises(SourceSyncConflict):
                self.controller.source_review.apply(
                    self.a.id, revision, ["application:API"], overwrites,
                )
        result = self.controller.source_review.apply(
            self.a.id, revision, ["application:API"], ["application:API"],
        )
        self.assertEqual(result["review"]["changes"], [])
        self.assertEqual(
            self.controller.configuration.settings(self.a.id)["applications"][0]["port"],
            9000,
        )

    def test_missing_or_unselected_change_cannot_be_applied(self):
        self._change_source()
        rev = self.review(self.a.id)["revision"]
        service = self.controller.source_review
        for items in ([], ["compose.yaml", "compose.yaml"], ["task:unknown"],
                      ["application:API", "task:deleted"]):
            with self.subTest(items=items), self.assertRaises(SourceReviewInvalid):
                service.apply(self.a.id, rev, items, [])
        with self.assertRaises(SourceReviewInvalid):
            service.apply(self.a.id, "wrong", ["application:API"], [])
        (self.a_source / "tasks.toml").write_text(self._lines("tasks=[]"))
        revised = self.review(self.a.id)
        with self.assertRaises(SourceReviewInvalid):
            service.apply(self.a.id, revised["revision"], ["task:tests"], [])

    def test_compose_apply_is_validated_and_uses_private_workspace(self):
        self._change_source()
        rev = self.review(self.a.id)["revision"]
        with patch("devstack.control.docker._execute", return_value="") as validate:
            applied = self.controller.source_review.apply(
                self.a.id, rev, ["compose.yaml"], [],
            )
        self.assertEqual(validate.call_count, 1)
        self.assertNotIn("compose.yaml", [item["id"] for item in applied["review"]["changes"]])
        self.assertIn("python:3.13", self.controller.configuration.compose(self.a.id)["content"])
        self.assertIn("python:3.12", self.controller.configuration.compose(self.b.id)["content"])
        self.assertTrue(
            (self.root / "workspaces" / self.a.id / "baseline" / "compose.yaml").is_file()
        )

    def test_active_other_project_blocks_all_config_apply(self):
        self._change_source()
        rev = self.review(self.a.id)["revision"]
        self.controller.coordinator.activate(self.b.id)
        with self.assertRaises(Exception):
            self.controller.source_review.apply(
                self.a.id, rev, ["application:API"], [],
            )
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.b.id)
        self.assertEqual(self.controller.configuration.settings(self.a.id)["applications"][0]["port"],
                         8000)
        self.controller.coordinator.deactivate()

    def test_crash_after_sql_commit_replays_journal_before_activation(self):
        self._change_source()
        revision = self.review(self.a.id)["revision"]
        with patch.object(self.controller.source_review, "_publish",
                          side_effect=OSError("simulated failure after commit")):
            with self.assertRaises(OSError):
                self.controller.source_review.apply(
                    self.a.id, revision, ["application:API", "task:tests"], [],
                )
        with self.controller.registry.database.transaction() as connection:
            pending = connection.execute(
                "SELECT payload FROM source_sync_pending WHERE project_id=?", (self.a.id,),
            ).fetchone()
        self.assertIsNotNone(pending)
        self.assertEqual(self.controller.configuration.settings(self.a.id)["applications"][0]["port"],
                         9000)
        with self.assertRaises(SourceSyncConflict):
            self.controller.handle({"op": "projects"})
        with self.assertRaises(Exception):
            self.controller.coordinator.activate(self.b.id)
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")
        result = self.controller.handle({"op": "reconcile"})
        self.assertEqual(result["phase"], "inactive")
        with self.controller.registry.database.transaction() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM source_sync_pending LIMIT 1",
            ).fetchone())
        self.assertNotIn("application:API", {
            item["id"] for item in self.review(self.a.id)["changes"]
        })
        self.assertNotIn("task:tests", {
            item["id"] for item in self.review(self.a.id)["changes"]
        })
        self.assertEqual(
            self.controller.configuration.settings(self.a.id)["tasks"][0]["command"],
            ["pytest", "-q"],
        )

    def test_v2_api_apply_csrf_and_explicit_confirmations(self):
        def transport(op, **fields):
            try:
                return self.controller.handle({"op": op, **fields})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc
        app = create_app({"TESTING": True, "SECRET_KEY": "test",
                          "SESSION_COOKIE_SECURE": False}, transport=transport)
        browser = app.test_client()
        self._change_source()
        page = browser.get("/projects/" + self.a.id + "/source-review")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Apply selected changes", page.data)
        with browser.session_transaction() as session:
            csrf = session["_csrf"]
        url = "/api/v2/projects/" + self.a.id + "/source-review/apply"
        revision = self.review(self.a.id)["revision"]
        payload = {"revision": revision, "selected": ["application:API"], "overwrites": []}
        self.assertEqual(browser.post(url, json=payload).status_code, 403)
        response = browser.post(url, json=payload,
                                headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["applied"], ["application:API"])
        self.assertEqual(browser.post(
            "/api/v2/projects/" + self.b.id + "/source-review/apply",
            json=payload, headers={"X-CSRF-Token": csrf},
        ).status_code, 409)


if __name__ == "__main__":
    unittest.main()
