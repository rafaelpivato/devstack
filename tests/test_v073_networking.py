"""v0.7.3 public/private networking and delivery boundary regressions."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from devstack import networking
from devstack.control.ingress_ipc import IngressControlServer
from devstack.public_app import ingress_call, _target_url, create_app, IngressRejected
from devstack import public_app
from starlette.requests import Request


HOST = "devstack.example.ts.net"


class NetworkStates(unittest.TestCase):
    def test_legacy_and_new_exact_states(self):
        for old_mcp in (False, True):
            for old_hooks in (False, True):
                raw = networking.expected(HOST, old_private=True, old_hooks=old_hooks,
                                           old_mcp=old_mcp)
                self.assertEqual(networking.classify(HOST, raw),
                                 ("legacy", old_hooks, old_mcp))
            for public in (False, True):
                raw = networking.expected(HOST, new_private=True,
                                           public=public, old_mcp=old_mcp)
                self.assertEqual(networking.classify(HOST, raw)[0],
                                 "public_ready" if old_mcp and public
                                 else "new" if public else "private_ready")
                if public:
                    handlers = raw["Web"][HOST + ":443"]["Handlers"]
                    self.assertEqual(handlers, {"/": {"Proxy": networking.PUBLIC}})
                    self.assertNotIn(networking.PRIVATE, str(handlers))
                    expected_public = {HOST + ":443": True}
                    if old_mcp:
                        expected_public[HOST + ":10000"] = True
                    self.assertEqual(raw["AllowFunnel"], expected_public)
                self.assertNotIn(HOST + ":8443", raw["AllowFunnel"])
        with self.assertRaises(networking.MCPManagementError):
            networking.classify(HOST, {"Web": {HOST + ":443": {
                "Handlers": {"/": {"Proxy": networking.PRIVATE}}}},
                "AllowFunnel": {HOST + ":443": True}})

    @patch.object(networking, "log_event")
    @patch.object(networking, "_finish_bootstrap")
    @patch.object(networking, "_ready_public")
    @patch.object(networking, "write_preferences")
    @patch.object(networking, "read_preferences", return_value=None)
    @patch.object(networking, "run_command")
    @patch.object(networking, "state")
    @patch.object(networking, "pinned_hostname", return_value=HOST)
    def test_automatic_migration_from_both_legacy_public_routes(
            self, pin, state, command, read, write, public, finish, log):
        state.side_effect = [
            ("legacy", True, True),
            ("legacy", False, True),
            ("dual_private", False, True),
            ("private_ready", False, True),
            ("public_ready", False, True),
            ("new", False, False),
        ]
        self.assertEqual(networking.reconcile(), "network_reconciled")
        write.assert_called_once_with({"mcp": True, "webhooks": True})
        command_args = [call.args for call in command.call_args_list]
        self.assertEqual(command_args[0][:4],
                         ("tailscale", "funnel", "--https=8443", "off"))
        self.assertEqual(command_args[1][:4],
                         ("tailscale", "serve", "--bg", "--https=8443"))
        self.assertEqual(command_args[2],
                         ("tailscale", "serve", "--https=443", "off"))
        self.assertIn(("tailscale", "funnel", "--yes", "--bg",
                       "--https=443", networking.PUBLIC), command_args)
        self.assertIn(("tailscale", "funnel", "--https=10000", "off"),
                      command_args)
        public.assert_called_once()
        finish.assert_called_once()

    def test_private_mode_rejects_unrecognized_routes(self):
        raw = networking.expected(HOST, new_private=True)
        raw["Web"][HOST + ":8443"]["Handlers"]["/"] = {"Proxy": "http://127.0.0.1:1234"}
        with self.assertRaises(networking.MCPManagementError):
            networking.classify(HOST, raw)


class PublicIngressTests(unittest.TestCase):
    def test_target_must_be_internal_validated_container(self):
        target = {"lease": "123", "host": "10.11.12.13",
                  "port": 8000, "destination": "/hooks/github"}
        self.assertEqual(_target_url(target), "http://10.11.12.13:8000/hooks/github")
        for ip in ("127.0.0.1", "169.254.1.1", "8.8.8.8", "0.0.0.0"):
            with self.subTest(ip=ip), self.assertRaises(IngressRejected):
                _target_url(target | {"host": ip})

    def test_public_default_denies_disabled_routes(self):
        app = create_app(mcp_enabled=False, webhooks_enabled=False)
        from starlette.testclient import TestClient
        with TestClient(app) as client:
            self.assertEqual(client.get("/").status_code, 404)
            self.assertEqual(client.post("/mcp").status_code, 404)
            self.assertEqual(client.post("/hooks/" + "0"*36 + "/github").status_code, 404)

    def test_mcp_is_separate_from_webhooks(self):
        app = create_app(mcp_enabled=True, webhooks_enabled=False)
        from starlette.testclient import TestClient
        with TestClient(app) as client:
            self.assertEqual(client.post("/hooks/" + "0"*36 + "/github").status_code, 404)
            self.assertEqual(client.get("/healthz").status_code, 200)


class IngressSocketTests(unittest.TestCase):
    def test_socket_exposes_only_admit_and_release(self):
        class Ingress:
            def begin_delivery(self, project_id, route):
                if (project_id, route) != ("good", "stripe"):
                    raise ValueError("Unknown")
                return {"lease": "123", "host": "10.0.0.2",
                        "port": 8000, "destination": "/hooks"}
            def finish_delivery(self, lease):
                self.finished = lease
        instance = Ingress()
        with tempfile.TemporaryDirectory() as tmp:
            location = Path(tmp)/"ingress.sock"
            with IngressControlServer(location, instance, group=None) as server:
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    with patch.object(public_app, "INGRESS_SOCKET", str(location)):
                        self.assertEqual(ingress_call("admit", project_id="good",
                            route="stripe")["host"], "10.0.0.2")
                        ingress_call("release", lease="123")
                        self.assertEqual(instance.finished, "123")
                        with self.assertRaises(IngressRejected):
                            ingress_call("status")
                finally:
                    server.shutdown()
                    worker.join(2)


if __name__ == "__main__":
    unittest.main()
