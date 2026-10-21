"""I3 CLI project and runtime acceptance through a real Unix socket.

Real controller and SQLite registry, disposable fake Docker backend; external
Docker/systemd acceptance remains in mandatory GitHub workflows.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from devstack.api.http import create_app
from devstack.cli import main as cli
from devstack.control.daemon import ControlServer, Controller
from devstack.ipc.client import ControllerUnavailable
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database


class FakeDocker:
    def __init__(self):
        self.containers = []
        self.events = []

    def owned(self):
        return list(self.containers)

    def validate(self, identifier):
        self.events.append(("validate", identifier))

    def start(self, identifier, generation):
        self.events.append(("start", identifier))
        self.containers.append({
            "id": str(uuid4()), "project": identifier,
            "generation": generation, "state": "running",
        })

    def verify(self, identifier, generation):
        assert any(c["project"] == identifier and c["generation"] == generation
                   for c in self.containers)

    def stop(self, identifier):
        self.events.append(("stop", identifier))
        self.containers = [c for c in self.containers if c["project"] != identifier]


class ProjectRuntimeCLITests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.backend = FakeDocker()
        self.controller = Controller(
            Database(root / "db" / "controller.sqlite3"),
            WorkspaceStore(root / "projects"), self.backend,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("smoke-a", "Smoke A")
        self.b = self.controller.provisioner.create_empty("smoke-b", "Smoke B")
        self.socket_path = root / "control.sock"
        self.server = ControlServer(self.socket_path, self.controller)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.shutdown)

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["--socket", str(self.socket_path), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_list_and_show_are_read_only(self):
        for args in (
            ("--json", "project", "list"),
            ("project", "--json", "list"),
            ("project", "list", "--json"),
        ):
            with self.subTest(args=args):
                code, out, err = self.run_cli(*args)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual({p["id"] for p in json.loads(out)["projects"]},
                                 {self.a.id, self.b.id})
        code, out, err = self.run_cli("project", "show", self.a.id, "--json")
        self.assertEqual((code, err), (0, ""))
        detail = json.loads(out)
        self.assertEqual(detail["project"]["slug"], "smoke-a")
        self.assertIn("repositories", detail)
        self.assertIn("applications", detail)
        code, out, err = self.run_cli("project", "show", self.b.id)
        self.assertEqual(code, 0, err)
        self.assertIn("Name: Smoke B", out)
        self.assertIn("Repositories: 0", out)
        code, out, err = self.run_cli("project", "list")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(self.a.id, out)
        self.assertIn(self.b.id, out)
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")
        self.assertEqual(self.backend.containers, [])

    def test_a_b_a_and_shared_web_view(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "test-secret",
                          "SESSION_COOKIE_SECURE": False,
                          "CONTROLLER_SOCKET": str(self.socket_path)})
        client = app.test_client()
        code, out, err = self.run_cli("project", "activate", self.a.id)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Runtime: active", out)
        self.assertIn(self.a.id, out)
        self.assertEqual(client.get("/api/v2/runtime").json["project_id"], self.a.id)
        self.assertEqual(len(self.backend.containers), 1)
        generation = self.controller.coordinator.status()["generation"]

        code, out, err = self.run_cli("project", "switch", self.b.id, "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["project_id"], self.b.id)
        self.assertEqual(client.get("/api/v2/runtime").json["project_id"], self.b.id)
        self.assertEqual({c["project"] for c in self.backend.containers}, {self.b.id})
        self.assertFalse(self.controller.coordinator.authorized(self.a.id, generation))

        code, out, err = self.run_cli("project", "switch", self.a.id)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual({c["project"] for c in self.backend.containers}, {self.a.id})
        self.assertEqual(client.get("/api/v2/runtime").json["project_id"], self.a.id)

        code, out, err = self.run_cli("project", "deactivate")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.backend.containers, [])
        code, out, err = self.run_cli("runtime", "reconcile", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["phase"], "inactive")
        self.assertIsNone(json.loads(out)["project_id"])
        self.assertEqual({p.id for p in self.controller.registry.list()},
                         {self.a.id, self.b.id})

    def test_explicit_uuid_conflict_and_unknown_projects(self):
        code, out, err = self.run_cli("project", "activate", self.a.id)
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("project", "activate", self.b.id)
        self.assertEqual(code, cli.EX_TEMPFAIL)
        self.assertEqual(out, "")
        self.assertIn("ControlError", err)
        code, out, err = self.run_cli("project", "switch", str(uuid4()))
        self.assertEqual(code, cli.EX_DATAERR)
        self.assertIn("ProjectNotFound", err)
        self.assertEqual({c["project"] for c in self.backend.containers}, {self.a.id})

    def test_missing_and_noncanonical_uuid_fails_before_ipc(self):
        for args in (
            ("project", "show", "smoke-a"),
            ("project", "activate", "smoke-b"),
            ("project", "switch", "00000000-0000-0000-0000-000000000000"),
            ("project", "show"),
            ("project", "deactivate", self.a.id),
            ("runtime", "activate", self.a.id),
        ):
            with self.subTest(args=args):
                with patch.object(cli, "ensure_compatible") as handshake:
                    with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as done:
                        cli.main(list(args))
                    self.assertEqual(done.exception.code, 2)
                    handshake.assert_not_called()

    def test_show_never_changes_active_project(self):
        self.run_cli("project", "activate", self.a.id)
        code, out, err = self.run_cli("project", "show", self.b.id)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(self.b.id, out)
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.a.id)

    def test_narrow_output_and_global_flag_positions(self):
        for width in (40, 60, 80):
            with self.subTest(width=width):
                with patch.object(cli.shutil, "get_terminal_size",
                                  return_value=os.terminal_size((width, 20))):
                    for args in (("project", "list"), ("project", "show", self.a.id)):
                        code, out, err = self.run_cli(*args)
                        self.assertEqual(code, 0, err)
                        self.assertTrue(all(len(x) <= width for x in out.splitlines()),
                                        (width, args, out))
        code, out, err = self.run_cli("project", "show", self.a.id,
                                       "--socket", str(self.socket_path), "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["project"]["id"], self.a.id)

    def test_long_unbroken_project_labels_wrap_without_splitting_uuid(self):
        from devstack.cli.main import _render_project_list
        value = {
            "projects": [
                {"name": "X" * 120, "slug": "a" * 63,
                 "id": str(uuid4())},
            ],
        }
        for width in (40, 60, 80):
            with patch.object(cli.shutil, "get_terminal_size",
                              return_value=os.terminal_size((width, 20))):
                output = _render_project_list(value)
            self.assertTrue(all(len(line) <= width for line in output.splitlines()))
            self.assertIn(value["projects"][0]["id"], output)

    def test_interrupted_transition_has_unknown_outcome(self):
        with (patch.object(cli, "ensure_compatible",
                           return_value={"major": 1, "minor": 0}),
              patch.object(cli, "send",
                           side_effect=ControllerUnavailable("private path"))):
            code, out, err = self.run_cli("project", "switch", self.b.id)
        self.assertEqual(code, cli.EX_UNAVAILABLE)
        self.assertEqual(out, "")
        self.assertIn("outcome is unknown", err)
        self.assertNotIn("private path", err)
        with (patch.object(cli, "ensure_compatible",
                           return_value={"major": 1, "minor": 0}),
              patch.object(cli, "send", side_effect=KeyboardInterrupt)):
            code, out, err = self.run_cli("project", "deactivate")
        self.assertEqual(code, 130)
        self.assertIn("outcome is unknown", err)

    def test_transition_returns_receipt_and_uses_bounded_ipc(self):
        receipt = {
            "id": str(uuid4()), "status": "succeeded",
            "result": {"phase": "active", "project_id": self.a.id},
        }
        with (patch.object(cli, "ensure_compatible",
                           return_value={"major": 1, "minor": 1}) as compat,
              patch.object(cli, "send", return_value=receipt) as sender):
            code, out, err = self.run_cli("project", "activate", self.a.id)
        self.assertEqual(code, 0, err)
        self.assertIn("Runtime: active", out)
        self.assertEqual(sender.call_args.args[1], "transition_submit")
        self.assertEqual(sender.call_args.kwargs["timeout"], cli.RECEIPT_TIMEOUT)
        self.assertEqual(sender.call_args.kwargs["action"], "activate")
        self.assertEqual(sender.call_args.kwargs["project_id"], self.a.id)
        self.assertEqual(compat.call_args.kwargs["minimum_minor"], 1)
        self.assertIn("transition request key", err)
        self.assertLess(cli.RECEIPT_TIMEOUT, 30)


if __name__ == "__main__":
    unittest.main()
