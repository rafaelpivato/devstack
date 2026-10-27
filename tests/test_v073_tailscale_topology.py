"""Strict network cutover: no public dashboard route, no foreign route edits."""
import unittest
from unittest.mock import patch
from devstack import mcp_management as manage
from devstack.tailscale_layout import Topology, config, identify, MCP_PUBLIC, WEBHOOK_PUBLIC

HOST = "example.tailnet.ts.net"


class TopologyTests(unittest.TestCase):
    def test_legacy_and_new_known_combinations(self):
        for hooks in (False, True):
            for mcp in (False, True):
                for private in ("443", "8443"):
                    with self.subTest(hooks=hooks, mcp=mcp, private=private):
                        entry = identify(HOST, config(HOST, private=private,
                                                       webhooks=hooks, mcp=mcp))
                        self.assertEqual((entry.phase, entry.webhooks, entry.mcp),
                                         ("legacy" if private == "443" else "new",
                                          hooks, mcp))

    def test_migration_checkpoints_and_resume_state(self):
        for old in (False, True):
            dual = config(HOST, private="8443", private_443=True, old_mcp=old)
            stage = identify(HOST, dual)
            self.assertEqual(stage.phase, "dual_private")
            # A dashboard handler remains private on both ports.
            self.assertEqual(dual["AllowFunnel"].get(HOST + ":443"), None)
            state = identify(HOST, config(HOST, private="8443", old_mcp=old))
            self.assertEqual(state.phase, "private_ready" if old else "new")
        both = config(HOST, private="8443", old_mcp=True, mcp=True)
        self.assertEqual(identify(HOST, both).phase, "public_ready")

    def test_no_root_or_dashboard_on_public_443(self):
        for hooks in (False, True):
            for mcp in (False, True):
                state = config(HOST, private="8443", webhooks=hooks, mcp=mcp)
                if hooks or mcp:
                    handlers = state["Web"][HOST + ":443"]["Handlers"]
                    self.assertNotIn("/", handlers)
                    self.assertNotIn("http://127.0.0.1:8765",
                                     str(handlers))
                    self.assertEqual(state["AllowFunnel"], {HOST + ":443": True})
                else:
                    self.assertNotIn(HOST + ":443", state["Web"])
                    self.assertNotIn(HOST + ":443", state["AllowFunnel"])

    def test_reject_unknown_routes_and_proxy_targets(self):
        safe = config(HOST, private="8443", mcp=True)
        bad = [
            safe | {"AllowFunnel": {HOST + ":8443": True}},
            safe | {"Services": {"svc:other": {}}},
            safe | {"Web": {HOST + ":8443": safe["Web"][HOST + ":8443"],
                             HOST + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}}},
            safe | {"Web": {HOST + ":8443": safe["Web"][HOST + ":8443"],
                             HOST + ":443": {"Handlers": {"/mcp": {"Proxy": "http://127.0.0.1:8765/mcp"}}}}},
            safe | {"TCP": {"8443": {"HTTPS": True}}},
        ]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                identify(HOST, item)

    def test_targets_restore_removed_mount_prefixes(self):
        self.assertTrue(MCP_PUBLIC.endswith("/mcp"))
        self.assertTrue(WEBHOOK_PUBLIC.endswith("/hooks/"))


class MigrationTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    @patch.object(manage, "run_command")
    @patch.object(manage, "topology")
    @patch.object(manage, "pinned_hostname", return_value=HOST)
    def test_enable_public_webhooks_is_path_scoped_and_preserves_mcp(self, pin, topology, command, urlopen):
        urlopen.return_value.__enter__.return_value.read.return_value = b'{"scope":"webhook-only"}'
        topology.side_effect = [Topology("new", False, True),
                                Topology("new", True, True)]
        self.assertIn("/hooks/<name>", manage.apply_action("enable_webhooks"))
        command.assert_called_once_with("tailscale", "funnel", "--yes", "--bg",
            "--https=443", "--set-path=/hooks/",
            "http://127.0.0.1:8767/hooks/", timeout=45)

    @patch.object(manage, "run_command")
    @patch.object(manage, "topology")
    @patch.object(manage, "pinned_hostname", return_value=HOST)
    def test_disable_new_public_webhooks_preserves_mcp(self, pin, topology, command):
        topology.side_effect = [Topology("new", True, True), Topology("new", False, True)]
        self.assertEqual(manage.apply_action("disable_webhooks"), "webhooks_disabled")
        command.assert_called_once_with("tailscale", "funnel", "--https=443",
                                         "--set-path=/hooks/", "off", timeout=30)

    @patch("urllib.request.urlopen", side_effect=OSError("down"))
    @patch.object(manage, "run_command")
    @patch.object(manage, "topology", return_value=Topology("new", False, True))
    @patch.object(manage, "pinned_hostname", return_value=HOST)
    def test_reject_unhealthy_public_webhook_listener(self, pin, topology, command, urlopen):
        with self.assertRaises(manage.MCPManagementError) as ctx:
            manage.apply_action("enable_webhooks")
        self.assertEqual(ctx.exception.code, "webhook_listener_not_ready")
        command.assert_not_called()

    @patch.object(manage, "log_event")
    @patch.object(manage, "run_command")
    @patch.object(manage, "topology")
    def test_cutover_prepares_private_before_public(self, topology, command, log):
        topology.side_effect = [
            Topology("legacy", False, True),
            Topology("dual_private", False, True),
            Topology("dual_private", False, True),
            Topology("private_ready", False, True),
            Topology("public_ready", False, True),
            Topology("public_ready", False, True),
            Topology("new", False, True),
        ]
        self.assertIn(":8443/", manage.migrate_ports(HOST))
        commands = [call.args for call in command.call_args_list]
        self.assertEqual(commands[0][:4],
                         ("tailscale", "serve", "--bg", "--https=8443"))
        self.assertEqual(commands[1],
                         ("tailscale", "serve", "--https=443", "off"))
        self.assertEqual(commands[2][:5],
                         ("tailscale", "funnel", "--yes", "--bg", "--https=443"))
        self.assertEqual(commands[3],
                         ("tailscale", "funnel", "--https=10000", "off"))
        self.assertIn("--set-path=/mcp", commands[2])
        self.assertIn(MCP_PUBLIC, commands[2])

    @patch.object(manage, "log_event")
    @patch.object(manage, "run_command")
    @patch.object(manage, "topology")
    @patch.object(manage, "pinned_hostname", return_value=HOST)
    def test_operator_can_disable_legacy_webhooks_without_mcp_loss(self, pin, topology, command, event):
        topology.side_effect = [Topology("legacy", True, True),
                                Topology("legacy", False, True)]
        self.assertEqual(manage.apply_action("disable_webhooks"), "webhooks_disabled")
        command.assert_called_once_with("tailscale", "funnel", "--https=8443",
                                         "off", timeout=30)
        event.assert_called_with("webhooks_disabled")

    @patch.object(manage, "run_command")
    @patch.object(manage, "topology", return_value=Topology("new", False, True))
    @patch.object(manage, "pinned_hostname", return_value=HOST)
    def test_disable_webhooks_noop_on_new_public_443_when_already_off(self, pin, topology, command):
        self.assertEqual(manage.apply_action("disable_webhooks"), "webhooks_disabled")
        command.assert_not_called()

    @patch.object(manage, "run_command")
    @patch.object(manage, "topology", return_value=Topology("legacy", True, False))
    def test_active_legacy_webhooks_block_cutover(self, topology, command):
        with self.assertRaises(manage.MCPManagementError) as caught:
            manage.migrate_ports(HOST)
        self.assertEqual(caught.exception.code, "legacy_webhooks_must_be_disabled")
        command.assert_not_called()

    @patch.object(manage, "run_command")
    @patch.object(manage, "topology", return_value=Topology("dual_private", False, False))
    def test_cutover_does_not_funnel_private_443_on_failed_removal(self, topology, command):
        command.side_effect = manage.MCPManagementError("tailscale_command_failed")
        with self.assertRaises(manage.MCPManagementError):
            manage.migrate_ports(HOST)
        self.assertEqual(command.call_args.args,
                         ("tailscale", "serve", "--https=443", "off"))


if __name__ == "__main__":
    unittest.main()
