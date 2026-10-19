"""ADR INV-01/03/09: isolated, revision-checked managed project configuration."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from devstack.control.daemon import Controller
from devstack.projects.configuration import ConfigurationConflict, ConfigurationInvalid
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database
from devstack.api.http import create_app
from devstack.api.client import ControllerRejected


class FakeBackend:
    def __init__(self):
        self.running = []
    def owned(self):
        return self.running[:]
    def validate(self, identifier):
        return None
    def start(self, identifier, generation):
        self.running = [{"id": "dummy", "project": identifier,
                         "generation": generation, "state": "running"}]
    def verify(self, identifier, generation):
        assert len(self.running) == 1
    def stop(self, identifier):
        self.running = []


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.backend = FakeBackend()
        self.controller = Controller(
            Database(root / "db" / "state.sqlite3"),
            WorkspaceStore(root / "projects"),
            self.backend,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "Project A")
        self.b = self.controller.provisioner.create_empty("beta", "Project B")
        self.a_folder = root / "projects" / self.a.id
        self.b_folder = root / "projects" / self.b.id

    def test_compose_save_is_revision_checked_and_scoped(self):
        service = self.controller.configuration
        old = service.compose(self.b.id)
        self.assertEqual(old["revision"], "none")
        source = "services:\n  web:\n    image: nginx:alpine\n"
        with patch("devstack.control.docker._execute", return_value=""):
            saved = service.save_compose(self.b.id, source, "none")
        self.assertNotEqual(saved["revision"], "none")
        self.assertEqual(saved["content"], source)
        self.assertFalse((self.a_folder / "managed" / "compose.yaml").exists())
        with self.assertRaises(ConfigurationConflict):
            with patch("devstack.control.docker._execute", return_value=""):
                service.save_compose(self.b.id, source + "# stale", "none")
        self.assertEqual(service.compose(self.b.id), saved)

    def test_settings_replacement_cannot_touch_another_project(self):
        service = self.controller.configuration
        applications = [{"name": "Web", "service": "web", "port": 8000}]
        tasks = [{"name": "tests", "service": "web", "mode": "exec",
                  "command": ["python", "-m", "unittest"]}]
        updated = service.replace_settings(self.b.id, applications, tasks)
        self.assertEqual(updated["applications"], applications)
        self.assertEqual(updated["tasks"][0]["command"], tasks[0]["command"])
        self.assertEqual(service.settings(self.a.id), {"applications": [], "tasks": []})
        self.assertEqual(
            service.settings(self.b.id),
            Controller(
                Database(self.controller.registry.database.path),
                WorkspaceStore(self.b_folder.parent),
                self.backend,
            ).configuration.settings(self.b.id),
        )

    def test_active_project_blocks_configuration_for_every_project(self):
        service = self.controller.configuration
        self.controller.coordinator.activate(self.a.id)
        with self.assertRaises(ConfigurationConflict):
            service.replace_settings(self.b.id, [], [])
        with self.assertRaises(ConfigurationConflict):
            service.save_compose(self.b.id, "services:\n  web:\n    image: nginx\n",
                                 "none")
        self.assertFalse((self.b_folder / "managed" / "compose.yaml").exists())
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.a.id)

    def test_invalid_schema_does_not_destroy_previous_settings(self):
        service = self.controller.configuration
        first = service.replace_settings(self.b.id, [{"name": "Web", "service": "web"}], [])
        for apps, tasks in (
            ([{"name": "Web", "service": "web", "port": 99999}], []),
            ([{"name": "Web", "service": "../bad"}], []),
            ([], [{"name": "bad", "service": "web", "command": "rm -rf"}]),
        ):
            with self.subTest(apps=apps, tasks=tasks), self.assertRaises(ConfigurationInvalid):
                service.replace_settings(self.b.id, apps, tasks)
            self.assertEqual(service.settings(self.b.id), first)

    def test_v2_api_edits_inactive_project_only(self):
        def transport(op, **fields):
            try:
                return self.controller.handle({"op": op, **fields})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc
        app = create_app({"TESTING": True, "SECRET_KEY": "test",
                          "SESSION_COOKIE_SECURE": False}, transport=transport)
        browser = app.test_client()
        page = browser.get("/projects/" + self.b.id)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Managed Docker Compose", page.data)
        with browser.session_transaction() as session:
            csrf = session["_csrf"]
        body = {"applications": [{"name": "API", "service": "api"}], "tasks": []}
        result = browser.put("/api/v2/projects/" + self.b.id + "/settings",
                             json=body, headers={"X-CSRF-Token": csrf})
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(
            browser.get("/api/v2/projects/" + self.a.id + "/settings").get_json(),
            {"applications": [], "tasks": []},
        )
        with patch("devstack.control.docker._execute", return_value=""):
            saved = browser.put(
                "/api/v2/projects/" + self.b.id + "/compose",
                json={"content": "services:\n  api:\n    image: nginx\n",
                      "revision": "none"},
                headers={"X-CSRF-Token": csrf},
            )
        self.assertEqual(saved.status_code, 200, saved.get_json())
        self.assertEqual(
            browser.get("/api/v2/projects/" + self.b.id + "/compose").get_json(),
            saved.get_json(),
        )
        self.assertFalse((self.a_folder / "managed" / "compose.yaml").exists())


if __name__ == "__main__":
    unittest.main()
