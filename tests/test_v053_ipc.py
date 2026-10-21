"""Regression tests for the shared web/SSH controller IPC foundation."""
from contextlib import contextmanager
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

from devstack.api import client as legacy_client
from devstack.ipc.client import (
    ControllerIncompatible, ControllerRejected, ControllerUnavailable,
    ensure_compatible, send, send_payload,
)
from devstack.ipc.protocol import (
    MAX_MESSAGE, OPERATIONS, ProtocolError, validate_request,
)
from devstack.control.daemon import ControlServer, request as old_request


class FakeController:
    def handle(self, data):
        operation = validate_request(data)
        if operation == "protocol_info":
            return {"major": 1, "minor": 0}
        if operation == "status":
            return {"phase": "inactive"}
        if operation == "projects":
            return {"projects": []}
        if operation == "health":
            return "x" * MAX_MESSAGE
        raise ProtocolError("Intentional test failure")


class InvalidReplyHandler(socketserver.StreamRequestHandler):
    payload = b"this is not JSON\n"

    def handle(self):
        self.rfile.readline(MAX_MESSAGE + 1)
        self.wfile.write(self.payload)


class SharedIPCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "controller.sock"

    @contextmanager
    def running(self, server):
        with server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield
            finally:
                server.shutdown()
                thread.join(3)

    def test_contract_is_immutable_and_exact(self):
        self.assertTrue({"health", "status", "projects", "protocol_info"} <= set(OPERATIONS))
        self.assertEqual(validate_request({"op": "branch_apply", "project_id": "p",
                                            "branch": "main", "repository_id": None,
                                            "revision": "x"}), "branch_apply")
        with self.assertRaises(TypeError):
            OPERATIONS["arbitrary"] = frozenset()
        for payload in (
            {"op": "exec", "argv": ["sh"]},
            {"op": "projects", "unexpected": 1},
            {"op": "activate"},
            {"op": "status", "project_id": None},
            {"op": 17},
            [],
        ):
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                validate_request(payload)

    def test_one_transport_and_compatibility_handshake(self):
        # No service or storage process is needed for a real Unix socket test.
        with self.running(ControlServer(self.path, FakeController())):
            self.assertEqual(send(self.path, "status"), {"phase": "inactive"})
            self.assertEqual(send_payload(self.path, {"op": "projects"}), {"projects": []})
            self.assertIs(legacy_client.send, send)
            self.assertIs(legacy_client.ControllerRejected, ControllerRejected)
            self.assertEqual(ensure_compatible(self.path), {"major": 1, "minor": 0})
            with self.assertRaises(ControllerIncompatible):
                ensure_compatible(self.path, minimum_minor=1)
            with self.assertRaises(ProtocolError):
                old_request(self.path, {"op": "github_ssh_status"})
            with self.assertRaises(ControllerRejected) as exc:
                send(self.path, "github_ssh_status")
            self.assertEqual(exc.exception.code, "ProtocolError")

    def test_server_rejects_unknown_or_extra_fields_before_execution(self):
        with self.running(ControlServer(self.path, FakeController())):
            with self.assertRaises(ControllerRejected) as exc:
                send(self.path, "status", argv=["/bin/sh"])
            self.assertEqual(exc.exception.code, "ProtocolError")
            with self.assertRaises(ControllerRejected):
                send_payload(self.path, {"op": "exec", "command": "whoami"})
            with self.assertRaises(ProtocolError):
                old_request(self.path, {"op": "exec", "command": "whoami"})

    def test_limit_requests_and_reply_size(self):
        with self.running(ControlServer(self.path, FakeController())):
            with self.assertRaises(ControllerRejected) as exc:
                send(self.path, "project_rename", project_id="x",
                     name="a" * MAX_MESSAGE)
            self.assertEqual(exc.exception.code, "RequestTooLarge")
            with self.assertRaises(ControllerRejected) as exc:
                send(self.path, "health")
            self.assertEqual(exc.exception.code, "ResponseTooLarge")

    def test_controller_unavailable_or_invalid_reply(self):
        with self.assertRaises(ControllerUnavailable):
            send(self.path, "status", timeout=.1)
        with self.running(socketserver.UnixStreamServer(str(self.path), InvalidReplyHandler)):
            with self.assertRaises(ControllerUnavailable):
                send(self.path, "status")

    def test_incompatible_controller_version(self):
        with self.running(ControlServer(self.path, FakeController())):
            with patch.object(FakeController, "handle",
                              return_value={"major": 2, "minor": 0}):
                with self.assertRaises(ControllerIncompatible):
                    ensure_compatible(self.path)

    def test_transport_has_no_privileged_imports(self):
        # The local socket client must not import controller internals.
        import sys
        self.assertNotIn("devstack.control.daemon", send.__module__)
        self.assertFalse(hasattr(legacy_client, "DockerBackend"))
        self.assertTrue("devstack.ipc.client" in sys.modules)


if __name__ == "__main__":
    unittest.main()
