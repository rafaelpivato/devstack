"""Issue #104: safe enrollment and post-install device naming."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from devstack.api.http import create_app

from devstack import tailscale_naming as naming, networking
from devstack.mcp_management import MCPManagementError

OLD = "old.tailnet.ts.net"
NEW = "new.tailnet.ts.net"
STATE = {"BackendState": "Running", "Self": {"ID": "node1", "DNSName": OLD + "."},
         "CurrentTailnet": {"Name": "tailnet"}}


class NamingTests(unittest.TestCase):
    def test_strict_labels(self):
        for name in ("a", "devstack", "dev-stack-01", "a" * 63):
            self.assertEqual(naming.validate_name(name), name)
        for name in ("", "a" * 64, "-bad", "bad-", "bad..name",
                     "Bad", "9bad", "localhost", "https://a", "a b",
                     "$(touch /tmp/owned)", "a;id", "a\nb", None, 7):
            with self.subTest(name=name), self.assertRaises(MCPManagementError):
                naming.validate_name(name)

    def test_live_status_uses_actual_dns_not_last_requested_name(self):
        with patch.object(naming, "json_command", return_value=STATE), patch.object(
                networking, "summary", return_value={"private_url": "https://old.tailnet.ts.net:8443/",
                                                       "url": None, "phase": "private_ready"}), patch.object(
                naming, "_safe_result", return_value=None):
            result = naming.status()
        self.assertEqual(result["name"], "old")
        self.assertEqual(result["fqdn"], OLD)
        self.assertEqual(result["private_url"], "https://" + OLD + ":8443/")

    def test_stale_tab_rejected_before_handoff(self):
        with patch.object(naming, "json_command", return_value=STATE):
            with self.assertRaises(MCPManagementError) as ctx:
                naming.request_rename("new", NEW)
        self.assertEqual(ctx.exception.code, "stale_hostname")

    def test_root_rename_preserves_routing_and_pinned_identity(self):
        with patch.object(networking, "network_lock") as lock, patch.object(
                naming, "pinned_hostname", side_effect=[OLD, NEW]), patch.object(
                naming, "json_command", return_value=networking.expected(OLD, new_private=True)), patch.object(
                networking, "classify", side_effect=[("private_ready", False, False),
                                                      ("private_ready", False, False)]), patch.object(
                networking, "read_preferences", return_value={"mcp": False, "webhooks": False}), patch.object(
                naming, "run_command") as cmd:
            from contextlib import nullcontext
            lock.return_value = nullcontext()
            self.assertEqual(naming._apply("new", OLD), ("renamed", NEW))
            cmd.assert_called_once_with("tailscale", "set", "--hostname=new", timeout=25)

    def test_failed_command_does_not_refresh_public_or_routes(self):
        with patch.object(networking, "network_lock") as lock, patch.object(
                naming, "pinned_hostname", return_value=OLD), patch.object(
                naming, "json_command", return_value=networking.expected(OLD, new_private=True)), patch.object(
                networking, "read_preferences", return_value={"mcp": False, "webhooks": False}), patch.object(
                naming, "run_command", side_effect=MCPManagementError("tailscale_command_timeout")), patch.object(
                networking, "_public_env") as env:
            from contextlib import nullcontext
            lock.return_value = nullcontext()
            with self.assertRaises(MCPManagementError):
                naming._apply("new", OLD)
            env.assert_not_called()

    def test_suffix_is_reported_not_assumed(self):
        suffixed = "new-1.tailnet.ts.net"
        with patch.object(networking, "network_lock") as lock, patch.object(
                naming, "pinned_hostname", side_effect=[OLD, suffixed]), patch.object(
                naming, "json_command", return_value={}), patch.object(
                networking, "classify", side_effect=[("private_ready", False, False),
                                                      ("private_ready", False, False)]), patch.object(
                networking, "read_preferences", return_value={"mcp": False, "webhooks": False}), patch.object(
                naming, "run_command"):
            from contextlib import nullcontext
            lock.return_value = nullcontext()
            self.assertEqual(naming._apply("new", OLD), ("renamed_with_suffix", suffixed))

    def test_result_scrubs_invalid_dns(self):
        with tempfile.TemporaryDirectory() as dir:
            result_path = Path(dir) / "result"
            result_path.write_text(json.dumps({"code": "renamed", "at": 7,
                "old_dns": "<unsafe>", "new_dns": NEW}))
            with patch.object(naming, "RESULT_FILE", result_path):
                result = naming._safe_result()
        self.assertIsNone(result["old_dns"])
        self.assertEqual(result["new_urls"]["mcp_url"], "https://" + NEW + "/mcp")

class NamingApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test",
                               "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_csrf"] = "fixed-token"
        self.headers = {"X-CSRF-Token": "fixed-token"}
        self.url = "/api/v2/devstack/network/tailscale/rename"

    def test_cannot_rename_without_csrf_or_with_invalid_payload(self):
        with patch("devstack.api.http.request_tailscale_rename") as request:
            body = {"name": "devstack", "expected_dns": OLD}
            self.assertEqual(self.client.post(self.url, json=body).status_code, 403)
            for invalid in ({"name": "devstack"},
                            {**body, "extra": "cmd"}, ["devstack", OLD],
                            {"name": 55, "expected_dns": OLD},
                            {"name": "x", "expected_dns": None}):
                with self.subTest(invalid=invalid):
                    self.assertEqual(self.client.post(
                        self.url, json=invalid, headers=self.headers).status_code, 400)
            request.assert_not_called()

    def test_rename_queues_only_exact_validated_shape(self):
        with patch("devstack.api.http.request_tailscale_rename") as request:
            data = {"name": "devstack", "expected_dns": OLD}
            response = self.client.post(self.url, json=data, headers=self.headers)
            self.assertEqual(response.status_code, 202)
            request.assert_called_once_with("devstack", OLD)

    def test_invalid_name_from_server_is_bounded(self):
        with patch("devstack.api.http.request_tailscale_rename",
                   side_effect=MCPManagementError("invalid_name")):
            response = self.client.post(self.url, json={
                "name": "bad;rm", "expected_dns": OLD}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("rm -", response.get_data(as_text=True))


class RootHandoffTests(unittest.TestCase):
    def test_root_worker_refuses_unowned_pending_file(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "tailscale-rename"
            marker.write_text(json.dumps({"name": "devstack", "expected_dns": OLD}))
            marker.chmod(0o644)  # never accept a world-readable request
            with patch.object(naming, "REQUEST_FILE", marker), patch.object(
                    naming, "RESULT_FILE", Path(folder) / "result"), patch.object(
                    naming.os, "geteuid", return_value=0), patch.object(
                    naming, "log_event"), patch.object(naming, "_apply") as apply:
                with self.assertRaises(MCPManagementError):
                    naming.apply_pending()
                apply.assert_not_called()

    def test_root_worker_uses_fixed_checked_payload_only(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "tailscale-rename"
            marker.write_text(json.dumps({"name": "devstack", "expected_dns": OLD}))
            marker.chmod(0o600)
            result_path = Path(folder) / "result"
            from types import SimpleNamespace
            account = SimpleNamespace(pw_uid=marker.stat().st_uid)
            with patch.object(naming, "REQUEST_FILE", marker), patch.object(
                    naming, "RESULT_FILE", result_path), patch.object(
                    naming.os, "geteuid", return_value=0), patch.object(
                    naming.pwd, "getpwnam", return_value=account), patch.object(
                    naming, "log_event"), patch.object(naming, "_apply",
                    return_value=("renamed", NEW)) as apply:
                naming.apply_pending()
            apply.assert_called_once_with("devstack", OLD)
            self.assertFalse(marker.exists())
            self.assertEqual(json.loads(result_path.read_text())["new_dns"], NEW)


class PropagationAndIsolationTests(unittest.TestCase):
    def test_pending_dns_is_not_reported_as_collision(self):
        from contextlib import nullcontext
        with patch.object(networking, "network_lock", return_value=nullcontext()), patch.object(
                naming, "pinned_hostname", return_value=OLD), patch.object(
                naming, "json_command", return_value=networking.expected(OLD, new_private=True)), patch.object(
                networking, "read_preferences", return_value={"mcp": False, "webhooks": False}), patch.object(
                naming, "run_command") as command, patch.object(
                naming.time, "sleep") as sleep, patch.object(
                networking, "_public_env") as env:
            with self.assertRaises(MCPManagementError) as exc:
                naming._apply("new", OLD)
        self.assertEqual(exc.exception.code, "dns_propagation_pending")
        command.assert_called_once_with("tailscale", "set", "--hostname=new", timeout=25)
        self.assertEqual(sleep.call_count, 5)
        env.assert_not_called()

    def test_unrecognized_serve_state_blocks_mutation(self):
        from contextlib import nullcontext
        with patch.object(networking, "network_lock", return_value=nullcontext()), patch.object(
                naming, "pinned_hostname", return_value=OLD), patch.object(
                naming, "json_command", return_value={"Foreign": "route"}), patch.object(
                naming, "run_command") as command:
            with self.assertRaises(MCPManagementError) as exc:
                naming._apply("new", OLD)
        self.assertEqual(exc.exception.code, "routes_unrecognized")
        command.assert_not_called()

    def test_out_of_band_rename_is_reflected_in_live_status(self):
        changed = {**STATE, "Self": {**STATE["Self"], "DNSName": NEW + "."}}
        with patch.object(naming, "json_command", return_value=changed), patch.object(
                networking, "summary", return_value={"private_url": "https://" + NEW + ":8443/",
                                                       "url": None, "phase": "private_ready"}), patch.object(
                naming, "_safe_result", return_value=None):
            self.assertEqual(naming.status()["fqdn"], NEW)

    def test_diagnostic_collector_includes_only_bounded_rename_unit(self):
        from devstack import mcp_management
        from unittest.mock import Mock
        with patch.object(mcp_management, "run_command", return_value=""), patch.object(
                mcp_management, "atomic_result_file") as record, patch(
                "devstack.diagnostics_bridge.capture_auxiliary", return_value={}), patch(
                "devstack.funnel_preflight.capture_preflight", return_value={}):
            mcp_management.capture_diagnostics()
        self.assertTrue(record.called)
        self.assertIsInstance(record.call_args.args[1]["events"], list)
        self.assertIn("devstack-tailscale-rename.service",
                      mcp_management.UNIT_FILES)
