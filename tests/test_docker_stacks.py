"""Tests for managed Compose configuration and lifecycle controls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from devstack import docker_stacks as d
from devstack.core import Project


class DockerStacksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspaces" / "my-project"
        self.root.mkdir(parents=True)
        p = patch("devstack.docker_stacks.load_projects",
                  return_value={"my-project": Project("my-project", (), ())})
        p.start()
        self.addCleanup(p.stop)
        p = patch("devstack.docker_stacks.Path.home",
                  return_value=Path(self.temp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_compose_definition_is_project_owned(self):
        path = d.compose_path("my-project")
        self.assertEqual(path, self.root / ".devstack" / "compose.yaml")
        self.assertFalse(path.exists())
        self.assertIn("nginx", d.read_compose("my-project"))

    @patch("devstack.docker_stacks.subprocess.run")
    def test_validate_then_save_and_keep_existing_on_error(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        d.save_compose("my-project", "services:\n  web:\n    image: nginx\n")
        before = d.read_compose("my-project")
        self.assertEqual(before, "services:\n  web:\n    image: nginx\n")
        self.assertEqual(d.compose_path("my-project").stat().st_mode & 0o777, 0o600)
        self.assertIn("config", run.call_args.args[0])
        run.return_value = subprocess.CompletedProcess([], 1, "", "bad YAML")
        with self.assertRaisesRegex(d.DockerError, "Invalid Compose"):
            d.save_compose("my-project", "invalid")
        self.assertEqual(d.read_compose("my-project"), before)

    def test_no_repo_contamination(self):
        repo = self.root / "api"
        repo.mkdir()
        with patch("devstack.docker_stacks.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            d.save_compose("my-project", "services:\n  web:\n    image: nginx\n")
        self.assertFalse((repo / ".devstack").exists())
        self.assertTrue((self.root / ".devstack" / "compose.yaml").is_file())

    def test_reject_escape_names_and_symlinks(self):
        for name in ("../evil", "-option", "a/b"):
            with self.assertRaises(d.DockerError):
                d.compose_path(name)
        (self.root / ".devstack").symlink_to(self.temp.name, target_is_directory=True)
        with self.assertRaisesRegex(d.DockerError, "symlink"):
            d.compose_path("my-project")

    @patch("devstack.docker_stacks.subprocess.Popen")
    def test_queued_only_allowlisted_operations(self, popen):
        d.managed_dir("my-project").mkdir()
        d.compose_path("my-project").write_text("services:\n  web:\n    image: nginx\n")
        with self.assertRaisesRegex(d.DockerError, "Unsupported"):
            d.queue_action("my-project", "exec --privileged")
        d.queue_action("my-project", "build")
        info = d.current_job("my-project")
        self.assertEqual(info["state"], "queued")
        self.assertEqual(info["action"], "build")
        self.assertEqual(len(info["id"]), 16)
        with self.assertRaisesRegex(d.DockerError, "already running"):
            d.queue_action("my-project", "up")
        self.assertEqual(popen.call_count, 1)

    @patch("devstack.docker_stacks.subprocess.run")
    def test_run_job_records_completion(self, run):
        d.managed_dir("my-project").mkdir()
        d.compose_path("my-project").write_text("services:\n  web:\n    image: nginx\n")
        folder = d._jobs_dir("my-project")
        d._write_job(folder, {"id": "a" * 16, "action": "up",
                              "state": "queued", "started": 123})
        run.return_value = subprocess.CompletedProcess([], 0)
        self.assertEqual(d.run_job("my-project", "up", "a" * 16), 0)
        info = d.current_job("my-project")
        self.assertEqual(info["state"], "success")
        self.assertEqual(info["exit_code"], 0)
        self.assertEqual(run.call_args.args[0][-3:],
                         ["up", "--detach", "--build"])

    def test_cannot_read_absent_project(self):
        with self.assertRaisesRegex(d.DockerError, "not found"):
            d.read_compose("unknown")

    def test_size_limit(self):
        with self.assertRaisesRegex(d.DockerError, "under 128"):
            d.save_compose("my-project", "x" * (d.MAX_COMPOSE_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
