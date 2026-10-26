"""MCP v0.7 guarded lifecycle: state machines and private action handoff."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import errno
import json
import os
import stat
import tempfile
import unittest

from devstack import mcp_management as manage
from devstack import mcp_diagnostics
from devstack.api.http import create_app


HOST = "mydev.my-tailnet.ts.net"


def served(webhooks=False, mcp=False):
    tcp = {"443": {"HTTPS": True}}
    web = {HOST + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}}
    allow = {}
    if webhooks:
        tcp["8443"] = {"HTTPS": True}
        web[HOST + ":8443"] = {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}}
        allow[HOST + ":8443"] = True
    if mcp:
        tcp["10000"] = {"HTTPS": True}
        web[HOST + ":10000"] = {"Handlers": {"/": {"Proxy": manage.TARGET}}}
        allow[HOST + ":10000"] = True
    return {"TCP": tcp, "Web": web, "AllowFunnel": allow}


class ServeStateTests(unittest.TestCase):
    def test_accept_private_and_managed_optional_routes(self):
        for hooks in (False, True):
            for mcp in (False, True):
                with self.subTest(hooks=hooks, mcp=mcp):
                    self.assertEqual(manage.serve_state(HOST, served(hooks, mcp)),
                                     (hooks, mcp))

    def test_foreign_route_refused_without_mutation(self):
        for state in (
            {}, served() | {"Other": "unknown"},
            served() | {"AllowFunnel": {HOST + ":443": True}},
            served() | {"Web": {HOST + ":443": {"Handlers":
                                  {"/": {"Proxy": "http://127.0.0.1:8768"}}}}},
            served(mcp=True) | {"TCP": {"443": {"HTTPS": True}}},
        ):
            with self.subTest(state=state), self.assertRaises(manage.MCPManagementError):
                manage.serve_state(HOST, state)

    def test_only_known_actions(self):
        with self.assertRaises(manage.MCPManagementError):
            manage.apply_action("deploy")
        with self.assertRaises(manage.MCPManagementError):
            manage.request_action("restart")

    def test_pinned_tailscale_identity(self):
        data = {"BackendState": "Running",
                "Self": {"DNSName": HOST + ".", "ID": "node"},
                "CurrentTailnet": {"Name": "tailnet"}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "identity.json"
            path.write_text(json.dumps({"node_id": "node", "tailnet": "tailnet"}))
            with patch.object(manage, "PIN_FILE", path), patch.object(
                manage, "json_command", return_value=data
            ):
                self.assertEqual(manage.pinned_hostname(), HOST)
                path.write_text(json.dumps({"node_id": "another", "tailnet": "tailnet"}))
                with self.assertRaises(manage.MCPManagementError):
                    manage.pinned_hostname()


class MarkerTests(unittest.TestCase):
    def test_only_two_actions_can_be_queued(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            path.chmod(0o700)
            with patch.object(manage, "REQUEST_DIR", path):
                manage.request_action("enable")
                marker = path / "mcp-action"
                self.assertEqual(marker.read_bytes(), b"enable")
                self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
                with self.assertRaises(manage.MCPManagementError):
                    manage.request_action("disable")
                marker.unlink()
                manage.request_action("disable")
                self.assertEqual(marker.read_bytes(), b"disable")

    def test_permission_errors_are_not_disguised_as_generic_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            path.chmod(0o700)
            with patch.object(manage, "REQUEST_DIR", path), patch.object(
                    manage.os, "open", side_effect=PermissionError(errno.EACCES, "denied")):
                with self.assertRaises(manage.MCPManagementError) as caught:
                    manage.request_action("enable")
                self.assertEqual(caught.exception.code, "request_permission_denied")

    def test_request_path_uses_verified_web_runtime_directory(self):
        self.assertEqual(manage.REQUEST_DIR, Path("/run/devstack-update-request"))
        self.assertIn("PathExists=/run/devstack-update-request/mcp-action",
                      manage.UNIT_FILES["devstack-mcp-apply.path"])

    def test_install_units_only_in_disconnected_state(self):
        with tempfile.TemporaryDirectory() as folder:
            units, requests = Path(folder)/"units", Path(folder)/"requests"
            units.mkdir()
            uid_gid = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch.object(manage.pwd, "getpwnam", return_value=uid_gid):
                manage.install_units(root=Path(folder), unit_dir=units,
                                     request_dir=requests, run_systemd=False)
                self.assertTrue(requests.is_dir())
                self.assertEqual(set(file.name for file in units.iterdir()),
                                 set(manage.UNIT_FILES))
                for name in manage.UNIT_FILES:
                    self.assertIn("# Managed by DevStack", (units/name).read_text())
                manage.install_units(root=Path(folder), unit_dir=units,
                                     request_dir=requests, run_systemd=False)
                (units/"devstack-mcp.service").write_text("Foreign service")
                with self.assertRaises(manage.MCPManagementError):
                    manage.install_units(root=Path(folder), unit_dir=units,
                                         request_dir=requests, run_systemd=False)


class WebAPIContractTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WEB_DIAGNOSTICS_PATH": ":memory:"})
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_csrf"] = "token"

    def test_csrf_and_bounded_request(self):
        with patch("devstack.api.http.request_mcp_action") as request:
            self.assertEqual(self.client.post("/api/v2/devstack/mcp/enable", json={}).status_code,403)
            headers = {"X-CSRF-Token": "token"}
            self.assertEqual(self.client.post("/api/v2/devstack/mcp/enable",
                                              json={}, headers=headers).status_code, 202)
            request.assert_called_once_with("enable")
            self.assertEqual(self.client.post("/api/v2/devstack/mcp/restart",
                                              json={}, headers=headers).status_code, 404)
            self.assertEqual(self.client.post("/api/v2/devstack/mcp/enable",
                                              json={"cmd": "sh"}, headers=headers).status_code, 400)
            request.assert_called_once()

    def test_status_has_no_sensitive_fields(self):
        safe={"gateway":"stopped","funnel":"disabled","url":None,
              "request_pending":False,"request_state":"ready","last_result":None,"installed":True}
        with patch("devstack.api.http.mcp_status", return_value=safe):
            response=self.client.get("/api/v2/devstack/mcp")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), safe)


class DiagnosticSnapshotTests(unittest.TestCase):
    def test_mcp_events_are_bounded_structured_and_private(self):
        now = manage.time.time_ns() // 1_000_000
        def entry(msg, source="devstack-mcp.service", priority="6"):
            return json.dumps({
                "__REALTIME_TIMESTAMP": str(now*1000),
                "_SYSTEMD_UNIT": source, "PRIORITY": priority,
                "MESSAGE": msg,
            })
        journal = "\n".join([
            entry("DEVSTACK_MCP gateway_started"),
            entry("DEVSTACK_MCP ping_ok"),
            entry("DEVSTACK_MCP funnel_error_funnel_permission_required",
                  "devstack-mcp-apply.service"),
            entry("private secret=ABC123 should never be in Web output",
                  "devstack-mcp.service", "3"),
            entry("error from unrelated host service", "ssh.service", "3"),
            entry("DEVSTACK_MCP ping_ok", "ssh.service"),
        ])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mcp-log.json"
            with patch.object(manage, "SNAPSHOT_FILE", path), patch.object(
                    manage, "run_command", return_value=journal):
                manage.capture_diagnostics()
            parsed = json.loads(path.read_text())
            self.assertEqual(len(parsed["events"]), 4)
            self.assertNotIn("ABC123", str(parsed))
            self.assertNotIn("ssh.service", str(parsed))
            values = mcp_diagnostics.snapshot(5, path)
            self.assertTrue(values["available"])
            self.assertEqual(len(values["events"]), 4)
            self.assertIn("mcp-funnel-error-funnel-permission-required",
                          [e["code"] for e in values["events"]])
            self.assertEqual(manage.ENV_FILE, Path("/etc/devstack-mcp.env"))
            self.assertIn("EnvironmentFile=/etc/devstack-mcp.env",
                          manage.UNIT_FILES["devstack-mcp.service"])

    def test_snapshot_absence_or_staleness_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "missing.json"
            self.assertFalse(mcp_diagnostics.snapshot(5, p)["available"])
            p.write_text(json.dumps({"generated_at": 1, "events": []}))
            self.assertFalse(mcp_diagnostics.snapshot(5, p)["available"])

    def test_action_result_is_valid_json_and_no_payload_leaks(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "last.json"
            with patch.object(manage, "RESULT_FILE", file):
                manage.result_record("enabled", url="https://mynode.ts.net:10000/mcp")
                self.assertEqual(json.loads(file.read_text())["code"], "enabled")


if __name__ == "__main__":
    unittest.main()
