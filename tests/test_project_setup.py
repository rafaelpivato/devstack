import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack.project_setup import (
    ProjectSetupError, add_github_repository, checked_name,
    create_project, github_ssh_url,
)
from devstack.core import load_projects


class ProjectSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "projects.toml"
        patcher = patch("devstack.project_setup.config_path", return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_two_repos_preserves_multiroot_project(self):
        create_project("demo")
        create_project("other")
        with self.assertRaises(ProjectSetupError):
            create_project("demo")
        data = load_projects(self.config)
        self.assertEqual(list(data), ["demo", "other"])
        self.assertEqual(data["demo"].repos, ())

    def test_github_url_normalization_and_rejection(self):
        for value in (
            "https://github.com/team/api",
            "git@github.com:team/api.git",
            "ssh://git@github.com/team/api.git",
        ):
            self.assertEqual(github_ssh_url(value),
                             ("git@github.com:team/api.git", "api"))
        for value in (
            "https://evil.example/team/api", "git@github.com:team/../../bad",
            "https://github.com/team/-bad", "-oProxyCommand=evil",
        ):
            with self.subTest(value=value), self.assertRaises(ProjectSetupError):
                github_ssh_url(value)

    def test_clone_failure_keeps_config_unchanged(self):
        create_project("demo")
        original = self.config.read_text()
        with patch("devstack.project_setup.git_environment", return_value={}), \
             patch("devstack.project_setup.ssh_command", return_value="ssh"), \
             patch("devstack.project_setup.subprocess.run", side_effect=OSError("offline")):
            with self.assertRaisesRegex(ProjectSetupError, "clone failed"):
                add_github_repository("demo", "https://github.com/example/first")
        self.assertEqual(self.config.read_text(), original)

    def test_multi_repo_clone_and_services_preserved(self):
        create_project("demo")
        # Path.home() is patched to keep temporary Git checkouts isolated.
        with patch("devstack.project_setup.Path.home",
                   return_value=Path(self.temp.name)), \
             patch("devstack.project_setup.git_environment", return_value={}), \
             patch("devstack.project_setup.ssh_command", return_value="ssh"), \
             patch("devstack.project_setup.subprocess.run") as runner:
            def fake_git(args, **kwargs):
                if args[:2] == ["git", "clone"]:
                    Path(args[-1]).mkdir(parents=True)
            runner.side_effect = fake_git
            add_github_repository("demo", "https://github.com/org/api")
            add_github_repository("demo", "git@github.com:org/web.git",
                                  branch="develop")
        projects = load_projects(self.config)
        self.assertEqual([r.name for r in projects["demo"].repos], ["api", "web"])
        self.assertEqual(len(projects["demo"].repos), 2)
        self.assertTrue(projects["demo"].repos[0].path.exists())
        with self.assertRaises(ProjectSetupError):
            add_github_repository("demo", "https://github.com/org/api",
                                  repo_name="api")
