"""Applications are an explicit project display list, not Docker discovery."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from devstack.applications import (
    Application, ApplicationError, add_application, update_application,
    remove_application, load_applications, registry_path, compose_states,
    application_status,
)
from devstack.core import Project


class ApplicationRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspaces" / "example"
        self.root.mkdir(parents=True)
        names = patch(
            "devstack.docker_stacks.load_projects",
            return_value={"example": Project("example", (), ())},
        )
        names.start()
        self.addCleanup(names.stop)
        home = patch("devstack.docker_stacks.Path.home", return_value=Path(self.temp.name))
        home.start()
        self.addCleanup(home.stop)

    def test_opt_in_only_and_no_repo_files_changed(self):
        repo = self.root / "web"
        repo.mkdir()
        self.assertEqual(load_applications("example"), ())
        add_application("example", "Frontend", "web", "5173")
        add_application("example", "Worker", "celery", "")
        apps = load_applications("example")
        self.assertEqual(apps, (
            Application("Frontend", "web", 5173),
            Application("Worker", "celery", None),
        ))
        self.assertTrue(registry_path("example").is_file())
        self.assertEqual(registry_path("example").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(repo.iterdir()), [])
        self.assertFalse((self.root / ".devstack" / "compose.yaml").exists())

    def test_edit_and_remove_are_metadata_only(self):
        add_application("example", "API", "api", "8000")
        update_application("example", "API", "Backend", "server", "")
        self.assertEqual(load_applications("example"), (Application("Backend", "server", None),))
        remove_application("example", "Backend")
        self.assertEqual(load_applications("example"), ())
        with self.assertRaisesRegex(ApplicationError, "not found"):
            remove_application("example", "Backend")

    def test_duplicate_name_rejected_and_original_preserved(self):
        add_application("example", "API", "api", "8000")
        with self.assertRaisesRegex(ApplicationError, "already exists"):
            add_application("example", "api", "another", "")
        with self.assertRaisesRegex(ApplicationError, "already exists"):
            add_application("example", "API", "another", "")
        self.assertEqual(len(load_applications("example")), 1)

    def test_invalid_values_are_rejected(self):
        for service in ("../secret", "-flag", "", "foo/bar", "a" * 81):
            with self.subTest(service=service), self.assertRaises(ApplicationError):
                add_application("example", "API", service, 80)
        for port in ("abc", "-5", "0", "65536", "12.5", " 3000 "):
            with self.subTest(port=port), self.assertRaises(ApplicationError):
                add_application("example", "API", "api", port)
        with self.assertRaises(ApplicationError):
            add_application("example", "Bad\nName", "api", "")

    def test_refuses_invalid_manifest_without_overwriting(self):
        p = registry_path("example")
        p.parent.mkdir()
        data = '[[applications]]\nname = "Valid"\nservice = "web"\nport = 8000\n'
        p.write_text(data)
        self.assertEqual(load_applications("example"), (Application("Valid", "web", 8000),))
        p.write_text(data + "unknown = 42\n")
        with self.assertRaisesRegex(ApplicationError, "require"):
            add_application("example", "Another", "api", "")
        self.assertEqual(p.read_text(), data + "unknown = 42\n")

    def test_symlink_is_rejected(self):
        external = Path(self.temp.name) / "external"
        external.write_text("# outside")
        folder = self.root / ".devstack"
        folder.mkdir()
        (folder / "project.toml").symlink_to(external)
        with self.assertRaises(ApplicationError):
            load_applications("example")
        self.assertEqual(external.read_text(), "# outside")


class ApplicationStateTests(unittest.TestCase):
    def setUp(self):
        self.project = "example"
        self.app = Application("API", "api", 8000)

    @patch("devstack.applications.project_root")
    @patch("devstack.applications.compose_path")
    @patch("devstack.applications.subprocess.run")
    def test_explicit_application_status_from_compose_json(self, run, path, root):
        path.return_value.is_file.return_value = True
        run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps([
                {"Service": "api", "State": "running", "Health": "healthy"},
                {"Service": "db", "State": "running", "Health": "healthy"},
                {"Service": "redis", "State": "running", "Health": ""},
                {"Service": "other", "State": "exited", "Health": ""},
            ]), ""
        )
        states = compose_states(self.project)
        self.assertEqual(application_status(states, self.app), "running")
        self.assertEqual(states["db"], "running")
        self.assertEqual(application_status(states, Application("Missing", "missing", None)), "stopped")
        self.assertIn("ps", run.call_args.args[0])
        self.assertNotIn("up", run.call_args.args[0])

    @patch("devstack.applications.project_root")
    @patch("devstack.applications.compose_path")
    @patch("devstack.applications.subprocess.run")
    def test_ndjson_status_and_unhealthy(self, run, path, root):
        path.return_value.is_file.return_value = True
        run.return_value = subprocess.CompletedProcess(
            [], 0, '{"Service":"api","State":"running","Health":"unhealthy"}\n'
            '{"Service":"db","State":"exited"}', ""
        )
        states = compose_states(self.project)
        self.assertEqual(application_status(states, self.app), "unhealthy")
        self.assertEqual(states["db"], "stopped")

    @patch("devstack.applications.compose_path")
    def test_no_compose_is_unavailable(self, path):
        path.return_value.is_file.return_value = False
        self.assertEqual(application_status(compose_states(self.project), self.app),
                         "unavailable")


if __name__ == "__main__":
    unittest.main()
