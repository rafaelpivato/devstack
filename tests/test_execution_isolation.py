"""Execution authorization and queued-job invalidation during shutdown."""
from __future__ import annotations

from contextlib import nullcontext
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack import command_runner, docker_stacks, runtime
from devstack.workspace import Project


class IsolationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / "workspaces" / "demo"
        (self.root / ".devstack").mkdir(parents=True)
        (self.root / ".devstack" / "compose.yaml").write_text(
            "services:\n  web:\n    image: python:3.12\n"
        )
        for target, value in [
            ("devstack.docker_stacks.load_projects", {"demo": Project("demo", ())}),
            ("devstack.docker_stacks.Path.home", self.home),
            ("devstack.runtime._folder", self.home),
            ("devstack.runtime.load_project", Project("demo", ())),
            ("devstack.command_runner.services", ("web",)),
        ]:
            p = patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def phase(self, phase: str):
        runtime._write({"phase": phase, "project": "demo"})

    def test_only_active_runtime_allows_launch(self):
        for phase in ("inactive", "activating", "deactivating", "error"):
            self.phase(phase)
            with self.subTest(phase=phase):
                with self.assertRaises(runtime.RuntimeControlError):
                    with runtime.admit("demo"):
                        self.fail("Denied runtime was admitted")
        self.phase("active")
        with runtime.admit("demo"):
            pass
        with self.assertRaises(runtime.RuntimeControlError):
            with runtime.admit("other"):
                pass

    @patch("devstack.command_runner.subprocess.Popen")
    def test_command_cannot_be_queued_while_inactive(self, popen):
        self.phase("inactive")
        with self.assertRaisesRegex(command_runner.CommandError, "only while active"):
            command_runner.start_command("demo", "web", "exec", "python --version")
        popen.assert_not_called()

    @patch("devstack.docker_stacks.subprocess.Popen")
    def test_compose_cannot_be_queued_while_inactive(self, popen):
        self.phase("inactive")
        with self.assertRaisesRegex(docker_stacks.DockerError, "only while active"):
            docker_stacks.queue_action("demo", "build")
        popen.assert_not_called()

    @patch("devstack.command_runner.subprocess.Popen")
    def test_queued_command_does_not_launch_after_deactivation(self, popen):
        self.phase("active")
        identifier = command_runner.start_command("demo", "web", "exec",
                                                  "python --version")
        self.phase("deactivating")
        self.assertEqual(command_runner.work("demo", identifier), 1)
        self.assertEqual(command_runner.get_job("demo", identifier)["state"], "cancelled")
        self.assertEqual(popen.call_count, 1)  # queue worker only; no Docker exec

    @patch("devstack.docker_stacks.subprocess.Popen")
    def test_queued_compose_job_does_not_launch_after_deactivation(self, popen):
        self.phase("active")
        docker_stacks.queue_action("demo", "restart")
        identifier = docker_stacks.current_job("demo")["id"]
        self.phase("deactivating")
        self.assertEqual(docker_stacks.run_job("demo", "restart", identifier), 1)
        self.assertEqual(docker_stacks.current_job("demo")["state"], "cancelled")
        self.assertEqual(popen.call_count, 1)

    @patch("devstack.command_runner.subprocess.Popen")
    def test_cancelled_queue_is_not_resurrected_by_later_activation(self, popen):
        self.phase("active")
        identifier = command_runner.start_command("demo", "web", "exec", "true")
        self.phase("deactivating")
        command_runner.cancel_all("demo", timeout=.1)
        self.assertEqual(command_runner.get_job("demo", identifier)["state"], "cancelled")
        self.phase("active")
        self.assertEqual(command_runner.work("demo", identifier), 2)
        self.assertEqual(popen.call_count, 1)

    @patch("devstack.docker_stacks.subprocess.Popen")
    def test_cancelled_compose_queue_not_resurrected(self, popen):
        self.phase("active")
        docker_stacks.queue_action("demo", "build")
        identifier = docker_stacks.current_job("demo")["id"]
        self.phase("deactivating")
        docker_stacks.cancel_all("demo", timeout=.1)
        self.assertEqual(docker_stacks.current_job("demo")["state"], "cancelled")
        self.phase("active")
        self.assertEqual(docker_stacks.run_job("demo", "build", identifier), 2)
        self.assertEqual(popen.call_count, 1)

    @patch("devstack.runtime._clean")
    @patch("devstack.runtime.subprocess.Popen")
    def test_deactivation_joins_both_worker_types(self, popen, clean):
        self.phase("active")
        command_runner.start_command("demo", "web", "exec", "true")
        docker_stacks.queue_action("demo", "build")
        runtime.request_transition("deactivate")
        token = runtime.status()["token"]
        self.assertEqual(runtime.perform("deactivate", "demo", token), 0)
        self.assertEqual(runtime.status()["phase"], "inactive")
        self.assertEqual(command_runner.recent_jobs("demo")[0]["state"], "cancelled")
        self.assertEqual(docker_stacks.current_job("demo")["state"], "cancelled")
        clean.assert_called_once_with("demo")


if __name__ == "__main__":
    unittest.main()
