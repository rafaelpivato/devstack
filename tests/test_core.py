import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack.core import (
    OperationError, Repo, Service, load_projects, repo_switch, repo_update,
    service_action, service_logs,
)


class ConfigTests(unittest.TestCase):
    def test_load_example(self):
        projects = load_projects(Path("examples/workspace.toml"))
        self.assertEqual(list(projects), ["demo"])
        self.assertEqual(len(projects["demo"].repos), 2)

    def test_reject_unknown_service_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.toml"
            path.write_text('[project]\nname="a"\n[[project.services]]\nname="b"\nkind="shell"\n')
            with self.assertRaises(ValueError):
                load_projects(path)

    def test_reject_compose_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.toml"
            path.write_text('[project]\nname="a"\n[[project.services]]\nname="b"\nkind="compose"\ncwd="."\ncompose_file="../elsewhere.yml"\n')
            with self.assertRaises(ValueError):
                load_projects(path)


class CommandTests(unittest.TestCase):
    @patch("devstack.core.run")
    def test_legacy_compose_execution_is_disabled(self, run):
        with self.assertRaisesRegex(OperationError, "disabled"):
            service_action(Service("web", "compose", Path("/tmp/project"), "compose.yml"), "start")
        run.assert_not_called()

    @patch("devstack.core.run")
    def test_systemd_logs(self, run):
        service_logs(Service("worker", "systemd", unit="worker.service"), tail=999)
        run.assert_called_once_with(
            ["journalctl", "--user", "-u", "worker.service",
             "--no-pager", "-n", "200", "--output=short-iso"], timeout=20,
        )

    @patch("devstack.core.repo_dirty", return_value=True)
    def test_refuse_dirty_repo_update(self, dirty):
        with self.assertRaises(OperationError):
            repo_update(Repo("api", Path("/tmp/api")))

    @patch("devstack.core.repo_dirty", return_value=False)
    @patch("devstack.core.run")
    def test_switch_rejects_option_injection(self, run, dirty):
        with self.assertRaises(OperationError):
            repo_switch(Repo("api", Path("/tmp/api")), "--force")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
