"""Import a Devstack project from a GitHub repository or tree URL."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack.template_import import (
    ImportError as TemplateImportError, parse_template_url, discover,
    resolve_tree, import_github_project,
)
from devstack.core import load_projects
from devstack.applications import load_applications, Application
from devstack.docker_stacks import compose_command


class ImportURLTests(unittest.TestCase):
    def test_repository_urls_and_subfolder_urls(self):
        self.assertEqual(parse_template_url("https://github.com/rafaelpivato/devstack"), (
            "git@github.com:rafaelpivato/devstack.git", "devstack", None,
        ))
        self.assertEqual(parse_template_url(
            "https://github.com/rafaelpivato/devstack/tree/main/examples/smoke"
        ), (
            "git@github.com:rafaelpivato/devstack.git", "devstack",
            "main/examples/smoke",
        ))
        self.assertEqual(parse_template_url("git@github.com:org/tool.git"), (
            "git@github.com:org/tool.git", "tool", None,
        ))
        for value in (
            "https://github.com/org/repo/tree/main/../secret",
            "https://github.com/org/repo/tree/main/./secret",
            "https://github.com/org/repo/tree/main/a//b",
            "https://github.com/org/repo/tree/main/x%2fy",
            "https://evil.test/org/repo",
            "https://github.com/org/repo?token=x",
        ):
            with self.subTest(value=value), self.assertRaises(TemplateImportError):
                parse_template_url(value)

    def test_discovery_requires_single_manifest_or_explicit_subdirectory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            one = root / "examples" / "smoke"
            (one / ".devstack").mkdir(parents=True)
            (one / ".devstack" / "project.toml").write_text("[[applications]]\nname='Web'\nservice='web'\n")
            self.assertEqual(discover(root, root, False), one)
            two = root / "examples" / "other"
            (two / ".devstack").mkdir(parents=True)
            (two / ".devstack" / "project.toml").write_text("# another")
            with self.assertRaisesRegex(TemplateImportError, "Multiple"):
                discover(root, root, False)
            self.assertEqual(discover(root, one, True), one)
            with self.assertRaisesRegex(TemplateImportError, "No .devstack"):
                discover(root, root, True)

    @patch("devstack.template_import._command")
    def test_tree_url_detects_slashes_in_branch_and_default(self, command):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            nested = repo / "examples" / "smoke"
            nested.mkdir(parents=True)
            command.side_effect = [
                "main\nfeature/smoke\n",
                "main",  # already checked out
            ]
            branch, selected = resolve_tree(repo, "main/examples/smoke")
            self.assertEqual(branch, "main")
            self.assertEqual(selected, nested)
            self.assertEqual(command.call_count, 2)

            command.reset_mock()
            command.side_effect = [
                "main\nfeature/smoke\n",
                "main",
                "",
            ]
            branch, selected = resolve_tree(repo, "feature/smoke/examples/smoke")
            self.assertEqual(branch, "feature/smoke")
            self.assertEqual(selected, nested)
            self.assertIn("switch", command.call_args.args[0])


class ImportProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / ".config" / "devstack" / "projects.toml"
        self.config.parent.mkdir(parents=True)
        self.source = self.home / "source-repo"
        sample = self.source / "examples" / "smoke"
        (sample / ".devstack").mkdir(parents=True)
        (sample / "app").mkdir()
        (sample / "app" / "app.py").write_text("print('ok')\n")
        (sample / ".devstack" / "project.toml").write_text(
            '[[applications]]\nname="Web"\nservice="web"\nport=8000\n'
        )
        (sample / ".devstack" / "compose.yaml").write_text(
            "services:\n  web:\n    build:\n      context: ../app\n"
        )
        (sample / ".devstack" / "tasks.toml").write_text(
            '[[tasks]]\nname="tests"\nservice="web"\nmode="exec"\n'
            'command=["python","-m","unittest"]\n'
        )
        self.addCleanup(patch.stopall)
        patch("devstack.template_import.Path.home", return_value=self.home).start()
        patch("devstack.docker_stacks.Path.home", return_value=self.home).start()
        patch("devstack.project_setup.config_path", return_value=self.config).start()
        patch("devstack.template_import.git_environment", return_value={}).start()
        patch("devstack.template_import.ssh_command", return_value="ssh -i /fake").start()
        patch("devstack.docker_stacks.load_projects",
              side_effect=lambda: load_projects(self.config)).start()

    def command(self, args, **kwargs):
        if args[:2] == ["git", "clone"]:
            shutil.copytree(self.source, args[-1])
            return ""
        if "for-each-ref" in args:
            return "main\n"
        if "branch" in args and "--show-current" in args:
            return "main"
        return ""

    @patch("devstack.template_import.subprocess.run")
    @patch("devstack.template_import._command")
    def test_import_root_discovers_smoke_and_preserves_context(self, command, run):
        command.side_effect = self.command
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        name, branch = import_github_project(
            "https://github.com/rafaelpivato/devstack", "smoke-git",
        )
        self.assertEqual((name, branch), ("smoke-git", ""))
        project = load_projects(self.config)[name]
        self.assertEqual([r.name for r in project.repos], ["devstack"])
        self.assertTrue(project.repos[0].path.exists())
        self.assertEqual(load_applications(name), (Application("Web", "web", 8000),))
        from devstack.command_runner import load_tasks, Task
        self.assertEqual(load_tasks(name), (
            Task("tests", "web", "exec", ("python", "-m", "unittest")),))
        root = self.home / "workspaces" / name
        self.assertTrue((root / ".devstack" / "compose.yaml").is_file())
        self.assertEqual(
            (root / ".devstack" / ".sync-base" / "project.toml").read_text(),
            (root / ".devstack" / "project.toml").read_text())
        self.assertEqual(
            (root / ".devstack" / ".sync-base" / "tasks.toml").read_text(),
            (root / ".devstack" / "tasks.toml").read_text())
        self.assertEqual((root / ".devstack" / "compose.yaml").read_text(),
                         (root / "devstack" / "examples" / "smoke" / ".devstack" / "compose.yaml").read_text())
        arguments = compose_command(name)
        self.assertEqual(arguments[0:2], ["docker", "compose"])
        self.assertEqual(
            arguments[arguments.index("--project-directory") + 1],
            str(root / "devstack" / "examples" / "smoke" / ".devstack")
        )
        self.assertEqual(
            arguments[arguments.index("-f") + 1],
            str(root / ".devstack" / "compose.yaml")
        )
        self.assertEqual((root / "devstack" / "examples" / "smoke" / "app" / "app.py").read_text(),
                         "print('ok')\n")
        self.assertFalse((root / "devstack" / "examples" / "smoke" / ".devstack" / "import.toml").exists())

    @patch("devstack.template_import.subprocess.run")
    @patch("devstack.template_import._command")
    def test_explicit_tree_import_and_collision_rolls_back(self, command, run):
        command.side_effect = self.command
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.assertEqual(import_github_project(
            "https://github.com/rafaelpivato/devstack/tree/main/examples/smoke",
            "new-smoke"), ("new-smoke", "main"))
        with self.assertRaisesRegex(TemplateImportError, "Only one project"):
            import_github_project(
                "https://github.com/rafaelpivato/devstack/tree/main/examples/smoke",
                "new-smoke")
        self.assertEqual(len(load_projects(self.config)), 1)
        scratch = [p.name for p in (self.home / "workspaces").iterdir() if p.name.startswith(".devstack-import-")]
        self.assertEqual(scratch, [])

    @patch("devstack.template_import.subprocess.run")
    @patch("devstack.template_import._command")
    def test_invalid_compose_prevents_any_project_creation(self, command, run):
        command.side_effect = self.command
        run.return_value = subprocess.CompletedProcess([], 1, "", "bad")
        with self.assertRaisesRegex(TemplateImportError, "validation failed"):
            import_github_project("https://github.com/rafaelpivato/devstack", "bad")
        self.assertEqual(load_projects(self.config), {})
        self.assertFalse((self.home / "workspaces" / "bad").exists())

    @patch("devstack.template_import.subprocess.run")
    @patch("devstack.template_import._command")
    def test_template_without_compose_still_remembers_source(self, command, run):
        command.side_effect = self.command
        (self.source / "examples" / "smoke" / ".devstack" / "compose.yaml").unlink()
        name, _ = import_github_project(
            "https://github.com/rafaelpivato/devstack", "no-compose",
        )
        from devstack.project_sync import source_directory
        path = self.home / "workspaces" / name / ".devstack"
        self.assertTrue((path / "import.toml").is_file())
        self.assertFalse((path / "compose.yaml").exists())
        self.assertTrue(source_directory(name).is_dir())


if __name__ == "__main__":
    unittest.main()
