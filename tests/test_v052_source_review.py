"""ADR INV-01/03/09/10: read-only three-way Git template review."""
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

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


if __name__ == "__main__":
    unittest.main()
