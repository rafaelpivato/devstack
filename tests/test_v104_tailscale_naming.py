"""Issue #104: safe enrollment and post-install device naming."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
