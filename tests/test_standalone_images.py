"""Standalone Docker image execution is opt-in and independent of Compose services."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack.command_runner import (
    CommandError, Task, _parse_tasks, load_tasks, save_task,
    start_command, start_task, work, get_job, _project_network,
)
from devstack.core import Project


class StandaloneImageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / "workspaces" / "demo"
        (self.root / ".devstack").mkdir(parents=True)
        (self.root / ".devstack" / "compose.yaml").write_text(
            "services:\n  web:\n    image: python:3.12\n"
        )
        p = patch("devstack.docker_stacks.load_projects", return_value={
            "demo": Project("demo", (), ())
        })
        p.start()
        self.addCleanup(p.stop)
        h = patch("devstack.docker_stacks.Path.home", return_value=self.home)
        h.start()
        self.addCleanup(h.stop)
        from contextlib import nullcontext
        admit = patch("devstack.runtime.admit", return_value=nullcontext())
        admit.start()
        self.addCleanup(admit.stop)
        active = patch("devstack.runtime.is_active", return_value=True)
        active.start()
        self.addCleanup(active.stop)

    def test_image_task_roundtrip_and_legacy_compose_tasks(self):
        save_task("demo", "lint", "", "run", "python --version",
                  image="python:3.12-alpine")
        save_task("demo", "db", "", "run", "pg_isready -h postgres",
                  image="postgres:16", network="project")
        save_task("demo", "legacy", "web", "exec", "python --version")
        self.assertEqual(load_tasks("demo"), (
            Task("lint", "", "run", ("python", "--version"), "python:3.12-alpine", "none"),
            Task("db", "", "run", ("pg_isready", "-h", "postgres"), "postgres:16", "project"),
            Task("legacy", "web", "exec", ("python", "--version")),
        ))
        self.assertEqual(_parse_tasks((self.root / ".devstack" / "tasks.toml").read_text()),
                         load_tasks("demo"))

    def test_reject_invalid_image_or_network_and_mixed_target_fields(self):
        for image in ("", "-privileged", "python;rm", "../etc/passwd",
                      "registry.example.org/foo//bar:latest", "bad image"):
            with self.subTest(image=image), self.assertRaises(CommandError):
                save_task("demo", "bad", "", "run", "true", image=image,
                          network="project" if image == "" else "none")
        for network in ("host", "bridge", "container:db", "none;echo"):
            with self.subTest(network=network), self.assertRaises(CommandError):
                save_task("demo", "bad", "", "run", "true", image="alpine:3.20",
                          network=network)
        with self.assertRaises(CommandError):
            save_task("demo", "bad", "web", "run", "true", image="alpine:3.20")
        with self.assertRaises(CommandError):
            save_task("demo", "bad", "", "exec", "true", image="alpine:3.20")
        with self.assertRaises(CommandError):
            _parse_tasks('[[tasks]]\nname="a"\nimage="alpine:3.20"\n'
                         'service="web"\ncommand=["true"]')

    @patch("devstack.command_runner.services")
    @patch("devstack.command_runner.subprocess.Popen")
    def test_isolated_image_runs_without_compose_or_service_lookup(self, popen, services):
        (self.root / ".devstack" / "compose.yaml").unlink()
        token = start_command("demo", "", "run", "python --version",
                              image="python:3.12-alpine")
        services.assert_not_called()
        self.assertEqual(get_job("demo", token)["image"], "python:3.12-alpine")
        self.assertEqual(get_job("demo", token)["network"], "none")
        proc = popen.return_value
        proc.poll.return_value = 0
        proc.returncode = 0
        self.assertEqual(work("demo", token), 0)
        argv = popen.call_args.args[0]
        self.assertEqual(argv, [
            "docker", "run", "--rm", "--name", "devstack-task-" + token,
            "--label", "devstack.project=" + __import__(
                "devstack.docker_stacks", fromlist=["compose_name"]).compose_name("demo"),
            "--network", "none", "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL", "python:3.12-alpine",
            "python", "--version",
        ])
        self.assertNotIn("--volume", argv)
        self.assertNotIn("--env-file", argv)
        self.assertEqual(get_job("demo", token)["state"], "success")

    @patch("devstack.command_runner.subprocess.run")
    @patch("devstack.command_runner.subprocess.Popen")
    def test_project_network_is_discovered_by_labels(self, popen, run):
        network_id = "a" * 64
        run.return_value = subprocess.CompletedProcess([], 0, network_id + "\n", "")
        token = start_command("demo", "", "run", "redis-cli -h redis ping",
                              image="redis:7-alpine", network="project")
        proc = popen.return_value
        proc.poll.return_value = 0
        proc.returncode = 0
        self.assertEqual(work("demo", token), 0)
        self.assertEqual(popen.call_args.args[0][
            popen.call_args.args[0].index("--network") + 1
        ], network_id)
        filters = run.call_args.args[0]
        self.assertIn("--filter", filters)
        self.assertIn("label=com.docker.compose.network=default", filters)
        self.assertTrue(any("label=com.docker.compose.project=" in token
                            for token in filters))

    @patch("devstack.command_runner.subprocess.run")
    def test_network_missing_or_ambiguous_is_rejected(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        with self.assertRaisesRegex(CommandError, "unavailable or ambiguous"):
            _project_network("demo")
        run.return_value = subprocess.CompletedProcess([], 0, "a" * 64 + "\n" + "b" * 64, "")
        with self.assertRaises(CommandError):
            _project_network("demo")
        (self.root / ".devstack" / "compose.yaml").unlink()
        with self.assertRaisesRegex(CommandError, "Compose stack"):
            _project_network("demo")
        with self.assertRaisesRegex(CommandError, "Compose stack"):
            start_command("demo", "", "run", "true", image="alpine:3.20",
                          network="project")

    @patch("devstack.command_runner.services")
    @patch("devstack.command_runner.subprocess.Popen")
    def test_saved_image_task_starts_from_image_record(self, popen, services):
        save_task("demo", "lint", "", "run", "python --version", image="python:3.12")
        token = start_task("demo", "lint")
        self.assertEqual(get_job("demo", token)["image"], "python:3.12")
        services.assert_not_called()


    @patch("devstack.command_runner._project_network",
           side_effect=CommandError("Compose default network unavailable"))
    @patch("devstack.command_runner.subprocess.Popen")
    def test_missing_project_network_records_failure_not_stuck(self, popen, network):
        token = start_command("demo", "", "run", "redis-cli ping",
                              image="redis:7-alpine", network="project")
        self.assertEqual(work("demo", token), 1)
        self.assertEqual(get_job("demo", token)["state"], "failed")
        self.assertEqual(get_job("demo", token)["exit_code"], 1)
        network.assert_called_once_with("demo")
        # The only Popen call is queueing the worker; no Docker run started.
        self.assertEqual(popen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
