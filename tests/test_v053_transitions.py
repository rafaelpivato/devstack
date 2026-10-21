"""I4: crash-safe, idempotent runtime transitions on the real Unix controller IPC."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from devstack.cli import main as cli
from devstack.control.daemon import Controller, ControlServer
from devstack.control.transitions import TransitionConflict
from devstack.ipc.client import ControllerRejected, ControllerUnavailable, send
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database, SCHEMA_VERSION


class FakeBackend:
    def __init__(self):
        self.owned_workloads = []
        self.log = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def validate(self, identifier):
        return None

    def owned(self):
        return list(self.owned_workloads)

    def verify(self, identifier, generation):
        if not any(c["project"] == identifier and c["generation"] == generation
                   for c in self.owned_workloads):
            raise RuntimeError("Test Docker workload not present")

    def start(self, identifier, generation):
        self.entered.set()
        if self.block and not self.release.wait(6):
            raise RuntimeError("Test backend timed out")
        self.owned_workloads.append({"id": str(uuid4()), "project": identifier,
                                     "generation": generation, "state": "running"})
        self.log.append(("start", identifier))

    def stop(self, identifier):
        self.log.append(("stop", identifier))
        self.owned_workloads = [
            c for c in self.owned_workloads if c["project"] != identifier
        ]


class TransitionAcceptance(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.db = Database(root / "db" / "control.sqlite3")
        self.backend = FakeBackend()
        self.controller = Controller(self.db, WorkspaceStore(root / "projects"),
                                     self.backend)
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "Alpha")
        self.b = self.controller.provisioner.create_empty("beta", "Beta")
        self.socket_path = root / "control.sock"
        server = ControlServer(self.socket_path, self.controller)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        self.addCleanup(self.backend.release.set)

    def op(self, action, target=None, key=None):
        return send(self.socket_path, "transition_submit", action=action,
                    project_id=target, request_key=key or secrets.token_hex(16))

    def wait(self, receipt, limit=5):
        until = time.monotonic() + limit
        while time.monotonic() < until:
            receipt = send(self.socket_path, "transition_status", id=receipt["id"])
            if receipt["status"] not in {"queued", "running"}:
                return receipt
            time.sleep(.01)
        self.fail("Transition did not complete")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with (redirect_stdout(out), redirect_stderr(err),
              patch.object(cli, "POLL_SECONDS", .01)):
            code = cli.main(["--socket", str(self.socket_path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_accept_and_poll_is_independent_of_client_socket(self):
        self.backend.block = True
        accepted = self.op("activate", self.a.id)
        self.assertTrue(self.backend.entered.wait(3))
        self.assertEqual(accepted["status"], "running")
        self.assertEqual(send(self.socket_path, "status")["phase"], "activating")
        # Receipt reads are available even while Compose is blocked; after
        # original submitter closes its IPC connection the transition survives.
        self.assertEqual(self.controller.transitions.get(accepted["id"])["status"],
                         "running")
        with self.assertRaises(ControllerRejected) as conflict:
            self.op("switch", self.b.id)
        self.assertEqual(conflict.exception.code, "TransitionConflict")
        self.backend.release.set()
        finished = self.wait(accepted)
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["result"]["project_id"], self.a.id)
        self.assertNotIn("generation", finished["result"])
        self.assertEqual(self.backend.log, [("start", self.a.id)])

    def test_idempotency_and_explicit_request_key_recovery(self):
        key = secrets.token_hex(16)
        accepted = self.op("activate", self.a.id, key)
        duplicate = self.op("activate", self.a.id, key)
        self.assertEqual(accepted["id"], duplicate["id"])
        self.assertEqual(self.wait(duplicate)["status"], "succeeded")
        old_generation = self.controller.coordinator.status()["generation"]
        self.assertEqual(self.op("activate", self.a.id, key)["id"], accepted["id"])
        self.assertEqual(self.controller.coordinator.status()["generation"],
                         old_generation)
        self.assertEqual(self.backend.log, [("start", self.a.id)])
        with self.assertRaises(ControllerRejected) as err:
            self.op("switch", self.b.id, key)
        self.assertEqual(err.exception.code, "TransitionConflict")
        receipt = self.op("switch", self.b.id)
        self.assertEqual(self.wait(receipt)["status"], "succeeded")
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.b.id)

    def test_stale_submission_cannot_run_after_web_intervention(self):
        entered = threading.Event()
        release = threading.Event()
        supervisor = self.controller.transitions
        original = supervisor._work

        def delayed(identifier):
            entered.set()
            if not release.wait(6):
                raise RuntimeError("Test worker failed to unblock")
            return original(identifier)

        with patch.object(supervisor, "_work", side_effect=delayed):
            queued = self.op("switch", self.a.id)
            self.assertTrue(entered.wait(3))
            # Direct web v0.5.2 mutation uses the same global lock.
            send(self.socket_path, "activate", project_id=self.b.id, timeout=10)
            release.set()
            finished = self.wait(queued)
            self.assertEqual(finished["status"], "failed")
            self.assertEqual(finished["error_code"], "ControlError")
            self.assertEqual(self.controller.coordinator.status()["project_id"],
                             self.b.id)
            self.assertNotIn(("start", self.a.id), self.backend.log)

    def test_rollback_compatible_sidecar_and_restart_interrupted(self):
        running = self.op("activate", self.a.id)
        self.wait(running)
        with self.db.transaction() as con:
            version = con.execute("SELECT version FROM schema_version "
                                  "WHERE singleton=1").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)
        reopened = Database(self.db.path)  # v0.5.2 storage format is unchanged.
        self.assertEqual(reopened.path, self.db.path)
        with self.controller.transitions._transaction(write=True) as con:
            con.execute(
                "UPDATE transitions SET status='running',finished_ns=NULL WHERE id=?",
                (running["id"],),
            )
        # Equivalent to next daemon's boot-time journal reconciliation.
        self.assertEqual(self.controller.transitions.recover_interrupted(), 1)
        interrupted = self.controller.transitions.get(running["id"])
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["error_code"], "InterruptedTransition")
        self.controller.coordinator.reconcile()
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")
        self.assertEqual(self.backend.owned(), [])
        self.assertEqual(self.controller.transitions.recover_interrupted(), 0)
        self.assertEqual(self.backend.log.count(("start", self.a.id)), 1)

    def test_client_detach_and_lookup(self):
        code, out, err = self.run_cli(
            "project", "activate", self.a.id, "--detach", "--json")
        self.assertEqual(code, 0, err)
        receipt = json.loads(out)
        self.assertEqual(receipt["action"], "activate")
        self.assertIn("request key", err)
        self.assertEqual(self.wait(receipt)["status"], "succeeded")
        code, out, err = self.run_cli("runtime", "operation", receipt["id"], "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "succeeded")
        code, out, err = self.run_cli("runtime", "operations")
        self.assertEqual(code, 0, err)
        self.assertIn(receipt["id"], out)

    def test_cli_wait_and_reconcile(self):
        code, out, err = self.run_cli("project", "activate", self.a.id)
        self.assertEqual(code, 0, err)
        self.assertIn("Runtime: active", out)
        code, out, err = self.run_cli("project", "switch", self.b.id)
        self.assertEqual(code, 0, err)
        self.assertIn(self.b.id, out)
        code, out, err = self.run_cli("runtime", "reconcile", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["phase"], "inactive")
        self.assertEqual(self.backend.owned(), [])

    def test_mutating_command_keys_are_validated(self):
        code, out, err = self.run_cli(
            "project", "activate", self.a.id,
            "--request-key", "not-a-valid-key")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        with self.assertRaises(ControllerRejected):
            self.op("activate", self.a.id, "bad")
        with self.assertRaises(ControllerRejected):
            send(self.socket_path, "transition_submit", action="exec",
                 project_id=None, request_key=secrets.token_hex(16))

    def test_connection_failure_after_submission_can_retry_by_key(self):
        from devstack.ipc.client import send as real_send
        key = secrets.token_hex(16)
        called = False

        def interrupted(socket_path, operation, **kwargs):
            nonlocal called
            result = real_send(socket_path, operation, **kwargs)
            if operation == "transition_submit" and not called:
                called = True
                raise ControllerUnavailable("Simulated lost acknowledgment")
            return result

        with patch.object(cli, "send", side_effect=interrupted):
            rc, out, err = self.run_cli(
                "project", "activate", self.a.id, "--request-key", key)
        self.assertEqual(rc, cli.EX_UNAVAILABLE)
        self.assertEqual(out, "")
        self.assertIn(key, err)
        self.assertEqual(self.wait(self.controller.transitions.list()["operations"][0])
                         ["status"], "succeeded")
        rc, out, err = self.run_cli(
            "project", "activate", self.a.id, "--request-key", key)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.backend.log.count(("start", self.a.id)), 1)


if __name__ == "__main__":
    unittest.main()
