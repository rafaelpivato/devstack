"""v0.5.2 native ad-hoc supervised jobs, history, bounded logs, cancellation.

Uses a fake Docker backend with real isolated process creation by the
controller worker. No host-shell or Docker command is accepted through IPC.
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from devstack.api.client import ControllerRejected
from devstack.api.http import create_app
from devstack.control.daemon import Controller, ProtocolError
from devstack.control.jobs import JobInvalid, JobError
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database


class Backend:
    def __init__(self):
        self.containers = []
        self.invocations = []
        self.fail_stop = False

    def owned(self):
        return list(self.containers)

    def validate(self, identifier):
        pass

    def start(self, identifier, generation):
        self.containers = [{"id": str(uuid4()), "project": identifier,
                            "generation": generation, "state": "running"}]

    def verify(self, identifier, generation):
        assert len(self.containers) == 1
        assert self.containers[0]["project"] == identifier
        assert self.containers[0]["generation"] == generation

    def stop(self, identifier):
        if self.fail_stop:
            raise RuntimeError("Cannot stop owned container")
        self.containers = []

    def task_command(self, identifier, task, generation, job_id):
        self.invocations.append((identifier, task, generation, job_id))
        # Emulate the Docker CLI's attached process using safe Python commands
        # in a real child process, so logs/timeouts and cancellation are real.
        return [sys.executable, "-c", task["argv"][-1]]

    def command(self, identifier):
        return [sys.executable, "-c", "print('compose')"]


class JobTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.backend = Backend()
        self.controller = Controller(
            Database(root / "registry.sqlite3"),
            WorkspaceStore(root / "projects"), self.backend,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "Alpha").id
        self.b = self.controller.provisioner.create_empty("beta", "Beta").id
        self.controller.coordinator.activate(self.a)
        self.root = root

        def transport(op, **args):
            try:
                return self.controller.handle({"op": op, **args})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc

        self.browser = create_app(
            {"TESTING": True, "SECRET_KEY": "test", "SESSION_COOKIE_SECURE": False},
            transport=transport,
        ).test_client()
        self.browser.get("/projects/" + self.a)
        with self.browser.session_transaction() as session:
            self.csrf = session["_csrf"]

    def _submit(self, identifier=None, *, kind="exec", service="web", image="",
                network="none", script="print('hello from container')"):
        if kind == "image":
            service, image = "", "python:3.12-alpine"
        return self.controller.jobs.submit_adhoc(
            identifier or self.a, kind, service, image, network,
            ["python", "-c", script],
        )

    def _wait(self, job_id, expected=None, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            row = self.controller.jobs.get(job_id)
            if expected and row["status"] == expected:
                return row
            if not expected and row["status"] in ("succeeded", "failed", "cancelled"):
                return row
            time.sleep(.02)
        self.fail("job did not transition within timeout")

    def test_compose_exec_and_run_and_standalone_image_use_one_supervisor(self):
        for kind in ("exec", "run", "image"):
            with self.subTest(kind=kind):
                ident = self._submit(kind=kind)
                self.assertEqual(self._wait(ident)["status"], "succeeded")
                row = self.controller.jobs.get(ident)
                self.assertEqual(row["kind"], "adhoc")
                text = self.controller.jobs.logs(self.a, ident)["text"]
                self.assertIn("hello from container", text)
                self.assertEqual(self.backend.invocations[-1][0], self.a)
                self.assertEqual(self.backend.invocations[-1][1]["mode"],
                                 "run" if kind == "image" else kind)
        history = self.controller.jobs.list(self.a)["jobs"]
        self.assertEqual(len(history), 3)
        self.assertEqual(self.controller.jobs.list(self.b)["jobs"], [])

    def test_invalid_argv_and_modes_rejected_before_process_admission(self):
        invalid = [
            ("exec", "", "", "none", ["echo"]),
            ("exec", "web", "", "project", ["echo"]),
            ("image", "web", "alpine", "none", ["echo"]),
            ("image", "", "https://invalid", "none", ["echo"]),
            ("host", "web", "", "none", ["echo"]),
            ("exec", "web", "", "none", []),
            ("exec", "web", "", "none", [""]),
            ("exec", "web", "", "none", ["x" * 4097]),
            ("exec", "web", "", "none", ["echo", 2]),
            ("exec", "web", "", "none", ["echo", "\x00"]),
        ]
        for kind,service,image,network,argv in invalid:
            with self.subTest(kind=kind, argv=argv), self.assertRaises(JobInvalid):
                self.controller.jobs.submit_adhoc(
                    self.a, kind, service, image, network, argv,
                )
        self.assertEqual(self.backend.invocations, [])

    def test_inactive_and_different_project_never_admitted(self):
        with self.assertRaises(Exception):
            self._submit(identifier=self.b)
        self.controller.coordinator.deactivate()
        with self.assertRaises(Exception):
            self._submit()
        self.assertEqual(self.controller.jobs.list(self.a)["jobs"], [])
        self.assertEqual(self.backend.invocations, [])

    def test_list_and_logs_project_bound_and_offset_checked(self):
        job_id = self._submit(script="print('abcdefgh')")
        self.assertEqual(self._wait(job_id)["status"], "succeeded")
        first = self.controller.jobs.logs(self.a, job_id, 0)
        second = self.controller.jobs.logs(self.a, job_id, 4)
        self.assertEqual(first["offset"], 0)
        self.assertEqual(second["offset"], 4)
        self.assertEqual(first["text"], "abcdefgh\n")
        self.assertEqual(second["text"], "efgh\n")
        self.assertTrue(first["complete"])
        for value in (-1, "0", 2097153, True):
            with self.assertRaises(JobInvalid):
                self.controller.jobs.logs(self.a, job_id, value)
        with self.assertRaises(JobError):
            self.controller.jobs.logs(self.b, job_id)
        with self.assertRaises(JobError):
            self.controller.jobs.cancel(self.b, job_id)

    def test_job_log_is_bounded_and_does_not_block_writer(self):
        job_id = self._submit(script="import sys;sys.stdout.write('x'*2200000)")
        self.assertEqual(self._wait(job_id)["status"], "succeeded")
        total = 0
        offset = 0
        while offset < 2097152:
            part = self.controller.jobs.logs(self.a, job_id, offset)
            self.assertLessEqual(len(part["text"]), 16384)
            if not part["text"]:
                break
            offset = part["next_offset"]
            total += len(part["text"])
        self.assertEqual(total, 2097152)
        self.assertEqual(self.controller.jobs.logs(self.a, job_id, offset)["text"], "")

    def test_cancel_running_adhoc_fences_all_execution_and_deactivates(self):
        job_id = self._submit(script="import time; time.sleep(60)")
        self._wait(job_id, "running")
        result = self.controller.jobs.cancel(self.a, job_id)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")
        self.assertFalse(self.backend.owned())
        with self.assertRaises(Exception):
            self._submit()
        self.controller.coordinator.activate(self.b)
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.b)
        self.controller.coordinator.deactivate()

    def test_cancel_fail_closed_on_docker_stop_failure(self):
        job_id = self._submit(script="import time; time.sleep(60)")
        self._wait(job_id, "running")
        self.backend.fail_stop = True
        with self.assertRaises(Exception):
            self.controller.jobs.cancel(self.a, job_id)
        self.assertEqual(self.controller.coordinator.status()["phase"], "error")
        with self.assertRaises(Exception):
            self.controller.coordinator.activate(self.b)
        self.backend.fail_stop = False
        self.controller.coordinator.reconcile()

    def test_http_history_logs_csrf_and_no_raw_host_proxy(self):
        job_id = self._submit()
        self._wait(job_id)
        uri = f"/api/v2/projects/{self.a}/jobs"
        self.assertEqual(self.browser.get(uri).status_code, 200)
        self.assertEqual(len(self.browser.get(uri).get_json()["jobs"]), 1)
        self.assertEqual(self.browser.get(
            f"/api/v2/projects/{self.b}/jobs/{job_id}/logs",
        ).status_code, 409)
        self.assertEqual(self.browser.get(
            f"/api/v2/projects/{self.a}/jobs/{job_id}/logs?offset=bad",
        ).status_code, 400)
        payload = {"kind": "run", "service": "web", "image": "",
                   "network": "none", "command": ["python", "-c", "print('api')"]}
        path = f"/api/v2/projects/{self.a}/adhoc"
        self.assertEqual(self.browser.post(path, json=payload).status_code, 403)
        self.assertEqual(self.browser.post(
            path, json={**payload, "argv": ["docker", "ps"]},
            headers={"X-CSRF-Token": self.csrf},
        ).status_code, 400)
        response = self.browser.post(
            path, json=payload, headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 202, response.get_json())
        new_job = response.get_json()["job_id"]
        self.assertEqual(self._wait(new_job)["status"], "succeeded")
        self.assertEqual(self.browser.get(
            f"/api/v2/projects/{self.a}/jobs/{new_job}/logs",
        ).status_code, 200)
        self.assertEqual(self.browser.get(
            f"/projects/{self.a}/jobs/{new_job}",
        ).status_code, 200)
        self.assertIn("Recent jobs", self.browser.get(
            f"/projects/{self.a}",
        ).get_data(as_text=True))
        with self.assertRaises(ProtocolError):
            self.controller.handle({"op": "adhoc", "project_id": self.a,
                "kind": "exec", "service": "web", "image": "",
                "network": "none", "command": ["echo"],
                "host_command": "sh"})

    def test_cancel_endpoint_and_cross_project_restriction(self):
        job_id = self._submit(script="import time;time.sleep(60)")
        self._wait(job_id, "running")
        cancel = f"/api/v2/projects/{self.a}/jobs/{job_id}/cancel"
        self.assertEqual(self.browser.post(cancel).status_code, 403)
        self.assertEqual(self.browser.post(
            f"/api/v2/projects/{self.b}/jobs/{job_id}/cancel",
            headers={"X-CSRF-Token": self.csrf},
        ).status_code, 409)
        response = self.browser.post(
            cancel, headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["status"], "cancelled")
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")


if __name__ == "__main__":
    unittest.main()
