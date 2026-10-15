"""v0.5 single-workspace persistence, isolation and management routes."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack.workspace import load_project, load_projects, Project
from devstack.project_setup import create_project, ProjectSetupError
from devstack.web import create_app


class SingleWorkspaceTests(unittest.TestCase):
    def test_unconfigured(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(load_project(Path(folder) / "absent.toml"))

    def test_exactly_one_not_an_array(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.toml"
            path.write_text('[project]\nname="demo"\n')
            self.assertEqual(load_project(path).name, "demo")
            self.assertEqual(len(load_projects(path)), 1)
            path.write_text('[[projects]]\nname="demo"\n[[projects]]\nname="other"\n')
            with self.assertRaises(ValueError):
                load_project(path)

    def test_duplicate_repositories_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.toml"
            path.write_text('[project]\nname="demo"\n'
                            '[[project.repos]]\nname="api"\npath="/tmp/api"\n'
                            '[[project.repos]]\nname="api"\npath="/tmp/other"\n')
            with self.assertRaisesRegex(ValueError, "Duplicate repository"):
                load_project(path)

    def test_invalid_names_and_unknown_keys_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.toml"
            for content in ('[project]\nname="../escape"\n', 
                            '[project]\nname="demo"\nmultiple=true\n'):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    load_project(path)

    def test_web_routes_are_not_project_named(self):
        app = create_app({"TESTING": True})
        rules = [r.rule for r in app.url_map.iter_rules()]
        self.assertNotIn("/projects/<name>/docker", rules)
        self.assertIn("/project/docker", rules)
        self.assertIn("/api/v1/project", rules)

    @patch("devstack.web.load_projects", return_value={})
    def test_unconfigured_project_api(self, _):
        app = create_app({"TESTING": True})
        self.assertEqual(app.test_client().get("/api/v1/project").json,
                         {"project": None})

    @patch("devstack.web.load_projects", return_value={"demo": Project("demo", ())})
    def test_selected_project_api(self, _):
        app = create_app({"TESTING": True})
        self.assertEqual(app.test_client().get("/api/v1/project").json["project"]["name"], "demo")


if __name__ == "__main__":
    unittest.main()
