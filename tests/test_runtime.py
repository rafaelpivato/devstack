"""Single runtime lifecycle state, locking, recovery and HTTP adapter."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack import runtime
from devstack.workspace import Project

ORIGINAL_VALIDATE = runtime._validated_compose


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        folder = patch("devstack.runtime._folder", return_value=self.root)
        project = patch("devstack.runtime.load_project",
                        return_value=Project("demo", ()))
        command = patch("devstack.runtime._validated_compose",
                        return_value=["docker", "compose", "-p", "demo"])
        stopper = patch("devstack.runtime._stop_jobs")
        for item in (folder, project, command, stopper):
            item.start()
            self.addCleanup(item.stop)

    def test_unprovisioned_state_is_inactive(self):
        self.assertEqual(runtime.status(), {"phase": "inactive", "project": None})

    @patch("devstack.runtime.subprocess.Popen")
    def test_activation_is_queued_with_single_worker(self, popen):
        record = runtime.request_transition("activate")
        self.assertEqual(record["phase"], "activating")
        self.assertEqual(runtime.status()["phase"], "activating")
        self.assertIn("--worker", popen.call_args.args[0])
        self.assertEqual(popen.call_count, 1)
        with self.assertRaisesRegex(runtime.RuntimeControlError, "already queued"):
            runtime.request_transition("activate")
        self.assertEqual(popen.call_count, 1)

    @patch("devstack.runtime._docker")
    @patch("devstack.runtime._clean")
    @patch("devstack.runtime.subprocess.Popen")
    def test_activate_only_reports_active_after_docker_success(self, popen, clean, docker):
        docker.return_value = subprocess.CompletedProcess([], 0, "", "")
        runtime.request_transition("activate")
        token = runtime.status()["token"]
        self.assertEqual(runtime.perform("activate", "demo", token), 0)
        self.assertEqual(runtime.status()["phase"], "active")
        self.assertNotIn("token", runtime.status())
        self.assertEqual(docker.call_args.args[0][-5:],
                         ["--detach", "--build", "--wait", "--wait-timeout", "180"])
        clean.assert_called_once_with("demo")

    @patch("devstack.runtime._clean")
    @patch("devstack.runtime._docker")
    @patch("devstack.runtime.subprocess.Popen")
    def test_failed_activate_never_reports_active(self, popen, docker, clean):
        docker.side_effect = runtime.RuntimeControlError("Docker stopped")
        runtime.request_transition("activate")
        self.assertEqual(runtime.perform("activate", "demo", runtime.status()["token"]), 1)
        self.assertEqual(runtime.status()["phase"], "error")
        self.assertEqual(clean.call_count, 2)

    @patch("devstack.runtime._clean")
    @patch("devstack.runtime.subprocess.Popen")
    def test_deactivate_preserves_project_data(self, popen, clean):
        runtime._write({"phase": "active", "project": "demo"})
        runtime.request_transition("deactivate")
        self.assertEqual(runtime.perform("deactivate", "demo", runtime.status()["token"]), 0)
        self.assertEqual(runtime.status()["phase"], "inactive")
        clean.assert_called_once_with("demo")

    @patch("devstack.runtime._clean")
    @patch("devstack.runtime.subprocess.Popen")
    def test_worker_token_prevents_replay(self, popen, clean):
        runtime.request_transition("activate")
        self.assertEqual(runtime.perform("activate", "demo", "0" * 24), 2)
        self.assertEqual(runtime.status()["phase"], "activating")
        clean.assert_not_called()

    @patch("devstack.runtime._clean")
    def test_reconcile_active_is_fail_closed(self, clean):
        runtime._write({"phase": "active", "project": "demo"})
        self.assertTrue(runtime.reconcile())
        clean.assert_called_once_with("demo")
        self.assertEqual(runtime.status()["phase"], "inactive")

    @patch("devstack.runtime._clean", side_effect=runtime.RuntimeControlError("no daemon"))
    def test_reconcile_failure_is_not_false_inactive(self, clean):
        runtime._write({"phase": "active", "project": "demo"})
        self.assertFalse(runtime.reconcile())
        self.assertEqual(runtime.status()["phase"], "error")

    @patch("devstack.runtime.subprocess.Popen")
    def test_shared_webhook_admissions_block_deactivation(self, popen):
        import threading
        import time
        runtime._write({"phase": "active", "project": "demo"})
        done = threading.Event()
        errors = []
        def deactivate():
            try:
                runtime.request_transition("deactivate")
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()
        with runtime.admit("demo", shared=True, blocking=True):
            with runtime.admit("demo", shared=True, blocking=True):
                worker = threading.Thread(target=deactivate, daemon=True)
                worker.start()
                self.assertFalse(done.wait(timeout=.2),
                                 "Deactivation crossed an in-flight webhook")
                self.assertEqual(runtime.status()["phase"], "active")
        worker.join(timeout=3)
        self.assertTrue(done.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(runtime.status()["phase"], "deactivating")
        with self.assertRaises(runtime.RuntimeControlError):
            with runtime.admit("demo", shared=True, blocking=True):
                pass

    @patch("devstack.runtime.subprocess.Popen")
    def test_mutex_blocks_concurrent_activation(self, popen):
        with runtime._locked():
            with self.assertRaisesRegex(runtime.RuntimeControlError, "in progress"):
                runtime.request_transition("activate")
        popen.assert_not_called()

    def test_state_corruption_rejected(self):
        (self.root / "runtime.json").write_text('{"phase":"active","project":[]}')
        with self.assertRaisesRegex(runtime.RuntimeControlError, "invalid"):
            runtime.status()

    @patch("devstack.runtime._docker")
    def test_restart_policies_are_rejected(self, docker):
        from devstack import docker_stacks
        compose = self.root / "compose.yaml"
        compose.write_text("services: {web: {image: nginx}}")
        docker.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"services": {"web": {"restart": "always"}}}), ""
        )
        with patch.object(docker_stacks, "compose_path", return_value=compose), \
             patch.object(docker_stacks, "compose_command", return_value=["docker", "compose"]):
            with self.assertRaisesRegex(runtime.RuntimeControlError, "restart policies"):
                ORIGINAL_VALIDATE("demo")

    @patch("devstack.runtime._owned_ids", side_effect=[["a" * 12], []])
    @patch("devstack.runtime._docker")
    def test_fallback_removes_only_project_owned_containers(self, docker, ids):
        from devstack import docker_stacks
        with patch.object(docker_stacks, "compose_path",
                          return_value=self.root / "missing.yaml"):
            runtime._clean("demo")
        docker.assert_called_once_with(["docker", "rm", "-f", "a" * 12], timeout=90)

    @patch("devstack.runtime._clean")
    @patch("devstack.runtime.subprocess.Popen")
    def test_deactivation_failure_is_error_not_inactive(self, popen, clean):
        runtime._write({"phase": "active", "project": "demo"})
        clean.side_effect = runtime.RuntimeControlError("busy")
        runtime.request_transition("deactivate")
        self.assertEqual(runtime.perform("deactivate", "demo", runtime.status()["token"]), 1)
        self.assertEqual(runtime.status()["phase"], "error")


class RuntimeHTTPTests(unittest.TestCase):
    def setUp(self):
        from devstack.web import create_app
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test"})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as s:
            self.csrf = s["_csrf"]

    @patch("devstack.web.runtime.status", return_value={"phase": "activating", "project": "demo", "token": "secret"})
    def test_api_does_not_expose_transition_token(self, status):
        response = self.client.get("/api/v1/runtime")
        self.assertEqual(response.json, {"phase": "activating", "project": "demo"})

    @patch("devstack.web.runtime.status", return_value={"phase": "inactive", "project": "demo"})
    def test_status_is_framework_contract(self, status):
        r = self.client.get("/api/v1/runtime")
        self.assertEqual(r.json["phase"], "inactive")

    @patch("devstack.web.load_projects", return_value={"demo": Project("demo", ())})
    @patch("devstack.web.runtime.request_transition")
    def test_transition_is_csrf_protected(self, transition, projects):
        self.assertEqual(self.client.post("/project/runtime/activate").status_code, 403)
        transition.assert_not_called()
        response = self.client.post("/project/runtime/activate",
                                    data={"_csrf": self.csrf})
        self.assertEqual(response.status_code, 302)
        transition.assert_called_once_with("activate")

    @patch("devstack.web.load_projects", return_value={})
    def test_transition_rejects_unconfigured_project(self, projects):
        self.assertEqual(self.client.post("/project/runtime/activate",
                         data={"_csrf": self.csrf}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
