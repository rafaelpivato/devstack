"""I5 native CLI services, logs, Compose and saved tasks through real IPC.

Uses a real Unix socket, actual SQLite project registry/coordinator, and a
disposable Docker backend; no Docker access or host shell in the CLI.
"""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from devstack.cli import main as cli
from devstack.control.daemon import ControlServer, Controller
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database
from devstack.ipc.client import ControllerRejected


CONTAINER_ID = "a" * 64
TASK_NAME = "unit-tests"


class FakeDocker:
    def __init__(self):
        self.containers = []
        self.log = []

    def owned(self):
        return list(self.containers)

    def validate(self, identifier):
        return None

    def start(self, identifier, generation):
        self.containers.append({
            "id": CONTAINER_ID, "project": identifier,
            "generation": generation, "state": "running",
        })

    def verify(self, identifier, generation):
        if not any(c["project"] == identifier and c["generation"] == generation
                   for c in self.containers):
            raise AssertionError("Test workload absent")

    def stop(self, identifier):
        self.containers = [c for c in self.containers if c["project"] != identifier]

    def service_snapshot(self, identifier, generation):
        self.log.append(("snapshot", identifier, generation))
        return {"services": [{
            "name": "web",
            "status": "running",
            "containers": [{"id": CONTAINER_ID, "state": "running",
                            "health": "healthy", "name": "web-1"}],
        }]}

    def service_logs(self, identifier, generation, container_id, tail):
        self.log.append(("logs", identifier, generation, container_id, tail))
        if container_id != CONTAINER_ID:
            raise RuntimeError("Unknown test container")
        return {"container_id": container_id, "service": "web",
                "text": "2026-10-21T03:00:00Z smoke ok\n",
                "truncated": False, "tail": tail}


class ServiceTaskCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.backend = FakeDocker()
        self.controller = Controller(
            Database(root / "db" / "controller.sqlite3"),
            WorkspaceStore(root / "workspaces"), self.backend,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "Alpha")
        self.b = self.controller.provisioner.create_empty("beta", "Beta")
        with self.controller.registry.database.transaction(write=True) as con:
            con.execute(
                "INSERT INTO tasks(project_id,name,service,mode,image,network,argv_json)"
                " VALUES(?,?,?,'exec','','none',?)",
                (self.a.id, TASK_NAME, "web", '["python","-V"]'),
            )
        self.socket = root / "controller.sock"
        server = ControlServer(self.socket, self.controller)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--socket", str(self.socket), *args])
        return rc, out.getvalue(), err.getvalue()

    def test_inactive_service_read_does_not_start_anything(self):
        rc, out, err = self.run_cli("service", "list", "--project", self.a.id)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("Runtime inactive", out)
        self.assertEqual(self.backend.log, [])
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")
        rc, out, err = self.run_cli(
            "service", "list", "--project", self.b.id, "--json")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out), {
            "project_id": self.b.id, "active": False, "services": [],
        })

    def test_active_services_logs_and_strict_cross_project_isolation(self):
        self.controller.coordinator.activate(self.a.id)
        generation = self.controller.coordinator.status()["generation"]
        for width in (40, 60, 80):
            with self.subTest(width=width):
                with patch.object(cli.shutil, "get_terminal_size",
                                  return_value=os.terminal_size((width, 24))):
                    rc, out, err = self.run_cli(
                        "service", "list", "--project", self.a.id)
                self.assertEqual((rc, err), (0, ""))
                self.assertIn("Service: web", out)
                self.assertIn("Health: healthy", out)
                self.assertTrue(all(len(line) <= width for line in out.splitlines()))
        rc, out, err = self.run_cli(
            "service", "list", "--project", self.a.id, "--json")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out)["services"][0]["name"], "web")
        self.assertIn(("snapshot", self.a.id, generation), self.backend.log)

        rc, out, err = self.run_cli(
            "service", "logs", CONTAINER_ID, "--project", self.a.id,
            "--tail", "25")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "2026-10-21T03:00:00Z smoke ok\n")
        self.assertIn(("logs", self.a.id, generation, CONTAINER_ID, 25),
                      self.backend.log)
        rc, out, err = self.run_cli(
            "--json", "service", "logs", CONTAINER_ID,
            "--project", self.a.id, "--tail", "3")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out)["tail"], 3)

        rc, out, err = self.run_cli(
            "service", "logs", CONTAINER_ID, "--project", self.b.id)
        self.assertEqual(rc, cli.EX_TEMPFAIL)
        self.assertEqual(out, "")
        self.assertIn("ControlError", err)
        self.assertFalse(any(item[:2] == ("logs", self.b.id)
                             for item in self.backend.log))

    def test_task_list_and_controller_owned_run(self):
        rc, out, err = self.run_cli("task", "list", "--project", self.a.id)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("Task: unit-tests", out)
        rc, out, err = self.run_cli(
            "task", "list", "--project", self.a.id, "--json")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out)["tasks"][0]["name"], TASK_NAME)
        rc, out, err = self.run_cli("task", "list", "--project", self.b.id)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("No saved tasks", out)

        # Admission remains controller-owned: even a configured task cannot
        # be submitted while inactive.
        rc, out, err = self.run_cli(
            "task", "run", TASK_NAME, "--project", self.a.id)
        self.assertEqual(rc, cli.EX_TEMPFAIL)
        self.assertEqual(out, "")
        self.assertIn("ControlError", err)

        self.controller.coordinator.activate(self.a.id)
        job_id = str(uuid4())
        with patch.object(self.controller.jobs, "submit_task", return_value=job_id) as task:
            rc, out, err = self.run_cli(
                "task", "run", TASK_NAME, "--project", self.a.id)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "Job: " + job_id + "\n")
        task.assert_called_once_with(self.a.id, TASK_NAME)
        with patch.object(self.controller.jobs, "submit_task", return_value=job_id):
            rc, out, err = self.run_cli(
                "task", "run", TASK_NAME, "--project", self.a.id, "--json")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out), {"job_id": job_id})

    def test_compose_read_and_safe_maintenance_job_submission(self):
        rc, out, err = self.run_cli("compose", "show", "--project", self.a.id, "--json")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("revision", json.loads(out))
        self.controller.coordinator.activate(self.a.id)
        job_id = str(uuid4())
        for action in ("build", "restart"):
            with self.subTest(action=action), patch.object(
                self.controller.jobs, "submit_compose", return_value=job_id,
            ) as submit:
                rc, out, err = self.run_cli(
                    "compose", action, "--project", self.a.id)
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(out, "Job: " + job_id + "\n")
                submit.assert_called_once_with(self.a.id, action)
        rc, out, err = self.run_cli("compose", "show", "--project", self.b.id)
        self.assertEqual(rc, 0, err)

    def test_invalid_identifiers_and_limits_never_contact_socket(self):
        cases = (
            ("service", "list"),
            ("service", "list", "--project", "alpha"),
            ("service", "logs", "deadbeef", "--project", self.a.id),
            ("service", "logs", CONTAINER_ID, "--project", self.a.id, "--tail", "0"),
            ("service", "logs", CONTAINER_ID, "--project", self.a.id, "--tail", "201"),
            ("service", "logs", CONTAINER_ID, "--project", self.a.id, "--tail", "nan"),
            ("task", "run", "unsafe..name", "--project", self.a.id),
            ("compose", "up", "--project", self.a.id),
            ("compose", "down", "--project", self.a.id),
            ("compose", "restart", "--project", "not-a-uuid"),
        )
        for args in cases:
            with self.subTest(args=args), patch.object(cli, "ensure_compatible") as handshake:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
                    cli.main(list(args))
                self.assertEqual(e.exception.code, 2)
                handshake.assert_not_called()

    def test_no_default_to_active_project(self):
        self.controller.coordinator.activate(self.a.id)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            cli.main(["service", "list"])
        self.assertEqual(e.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
