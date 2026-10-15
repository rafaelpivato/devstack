"""Review and selective application of repository-backed Devstack configuration."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack.core import Project, Repo
from devstack.applications import load_applications, Application
from devstack.command_runner import load_tasks, Task
from devstack.project_sync import (
    SyncError, review, apply, record_initial_baseline, source_directory,
)


APPS = '''[[applications]]
name = "API"
service = "web"
port = 8000
'''
TASKS = '''[[tasks]]
name = "tests"
service = "web"
mode = "run"
command = ["python", "-m", "unittest"]
'''
COMPOSE = '''services:
  web:
    image: python:3.12
'''


class ProjectSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        root = self.home / "workspaces" / "demo"
        self.root = root
        self.managed = root / ".devstack"
        self.managed.mkdir(parents=True)
        self.git_dir = root / "source" / "examples" / "smoke" / ".devstack"
        self.git_dir.mkdir(parents=True)
        (self.managed / "import.toml").write_text(
            '[origin]\nrepository="source"\nsubdirectory="examples/smoke"\n'
        )
        for filename, content in (
            ("project.toml", APPS), ("tasks.toml", TASKS), ("compose.yaml", COMPOSE)
        ):
            (self.managed / filename).write_text(content)
            (self.git_dir / filename).write_text(content)
        record_initial_baseline(self.managed, {
            "project.toml": APPS, "tasks.toml": TASKS, "compose.yaml": COMPOSE,
        })
        p = patch("devstack.docker_stacks.load_projects",
                  return_value={"demo": Project("demo",
                      (Repo("source", root / "source"),), ())})
        p.start()
        self.addCleanup(p.stop)
        h = patch("devstack.docker_stacks.Path.home", return_value=self.home)
        h.start()
        self.addCleanup(h.stop)

    def test_clean_import_has_no_changes(self):
        self.assertEqual(source_directory("demo"), self.git_dir)
        changes, fingerprint = review("demo")
        self.assertEqual(changes, [])
        self.assertEqual(len(fingerprint), 64)

    def test_new_tasks_selected_and_local_only_tasks_kept(self):
        local = TASKS + '''
[[tasks]]
name = "my-local"
service = "web"
mode = "exec"
command = ["python", "--version"]
'''
        (self.managed / "tasks.toml").write_text(local)
        upstream = TASKS + '''
[[tasks]]
name = "lint"
image = "python:3.12-alpine"
command = ["python", "--version"]
'''
        (self.git_dir / "tasks.toml").write_text(upstream)
        changes, fp = review("demo")
        self.assertEqual([(c.id, c.action, c.recommended) for c in changes],
                         [("tasks.toml:lint", "add", True)])
        self.assertEqual(apply("demo", ["tasks.toml:lint"], fp), 1)
        tasks = {task.name: task for task in load_tasks("demo")}
        self.assertEqual(set(tasks), {"tests", "my-local", "lint"})
        self.assertEqual(tasks["lint"].image, "python:3.12-alpine")
        self.assertEqual(review("demo")[0], [])

    def test_changed_source_item_stays_unselected_and_local_override_preserved(self):
        (self.git_dir / "tasks.toml").write_text(
            TASKS.replace('"python", "-m", "unittest"', '"python", "--version"')
        )
        local = TASKS.replace('"python", "-m", "unittest"', '"python", "-V"')
        (self.managed / "tasks.toml").write_text(local)
        changes, fp = review("demo")
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].locally_changed)
        self.assertFalse(changes[0].recommended)
        self.assertIn("python", changes[0].upstream)
        self.assertEqual((self.managed / "tasks.toml").read_text(), local)
        self.assertEqual(apply("demo", [], fp), 0)
        self.assertEqual((self.managed / "tasks.toml").read_text(), local)
        self.assertEqual(apply("demo", [changes[0].id], fp), 1)
        self.assertEqual(load_tasks("demo")[0].argv, ("python", "--version"))

    def test_applications_are_merged_by_name(self):
        upstream = APPS + '''
[[applications]]
name = "Frontend"
service = "client"
port = 5173
'''
        (self.git_dir / "project.toml").write_text(upstream)
        changes, token = review("demo")
        self.assertEqual([(c.kind,c.name,c.recommended) for c in changes],
                         [("Application", "Frontend", True)])
        apply("demo", [changes[0].id], token)
        self.assertEqual(load_applications("demo"), (
            Application("API", "web", 8000),
            Application("Frontend", "client", 5173),
        ))

    def test_compose_change_requires_explicit_selection_and_validation(self):
        upstream = COMPOSE.replace("python:3.12", "python:3.13")
        (self.git_dir / "compose.yaml").write_text(upstream)
        changes, token = review("demo")
        self.assertEqual(changes[0].kind, "Compose")
        self.assertFalse(changes[0].recommended)
        self.assertIn("+    image: python:3.13", changes[0].upstream)
        with patch("devstack.project_sync.save_compose") as save:
            self.assertEqual(apply("demo", [], token), 0)
            save.assert_not_called()
            self.assertEqual(apply("demo", ["compose.yaml"], token), 1)
            save.assert_called_once_with("demo", upstream)
        self.assertEqual(review("demo")[0][0].id, "compose.yaml")

    def test_stale_preview_cannot_apply(self):
        (self.git_dir / "tasks.toml").write_text(TASKS + '''
[[tasks]]
name="new"
service="web"
mode="run"
command=["true"]
''')
        changes, token = review("demo")
        (self.managed / "tasks.toml").write_text(TASKS + "# manual edit\n")
        with self.assertRaisesRegex(SyncError, "changed"):
            apply("demo", [changes[0].id], token)
        with self.assertRaisesRegex(SyncError, "Unknown"):
            apply("demo", ["tasks.toml:not-a-task"], review("demo")[1])

    def test_existing_import_without_baseline_is_conservative(self):
        import shutil
        shutil.rmtree(self.managed / ".sync-base")
        (self.git_dir / "tasks.toml").write_text(
            TASKS.replace('"python", "-m", "unittest"', '"python", "--version"')
        )
        changes, _ = review("demo")
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].locally_changed)
        self.assertFalse(changes[0].recommended)

    def test_no_project_origin_and_symlink_refusal(self):
        (self.managed / "import.toml").unlink()
        with self.assertRaisesRegex(SyncError, "no linked"):
            review("demo")
        (self.managed / "import.toml").write_text(
            '[origin]\nrepository="source"\nsubdirectory="examples/smoke"\n'
        )
        external = self.home / "outside"
        external.write_text("secret")
        (self.git_dir / "tasks.toml").unlink()
        (self.git_dir / "tasks.toml").symlink_to(external)
        with self.assertRaisesRegex(SyncError, "symlinked"):
            review("demo")
        self.assertEqual(external.read_text(), "secret")


if __name__ == "__main__":
    unittest.main()
