"""Verify bounded release hook: never perform Tailscale work inside the old updater deadline."""
import unittest
from unittest.mock import patch
from devstack.cli import install as cli


class UpgradeTests(unittest.TestCase):
    def test_postinstall_is_bounded_and_public_process_restarts(self):
        operations = []
        def record(label):
            def callback(*args, **kwargs):
                operations.append((label, args, kwargs))
                return False
            return callback

        with (patch.object(cli.os, "geteuid", return_value=0),
              patch.object(cli, "install_command", side_effect=record("cli")),
              patch.object(cli, "refresh_updater", side_effect=record("refresh")),
              patch("devstack.networking.prepare", side_effect=record("prepare")),
              patch("devstack.networking.reconcile", side_effect=AssertionError(
                  "Never synchronously reconcile inside the 25s old updater hook")),
              patch("devstack.mcp_management.install_units", side_effect=record("units")),
              patch.object(cli.subprocess, "run", side_effect=record("systemctl"))):
            self.assertEqual(cli.main([]), 0)
        labels = [entry[0] for entry in operations]
        self.assertLess(labels.index("refresh"), labels.index("prepare"))
        self.assertLess(labels.index("prepare"), labels.index("units"))
        commands = [args[0] for label, args, _ in operations if label == "systemctl"]
        self.assertIn(["systemctl", "--no-block", "try-restart",
                       "devstack-web-v2.service", "devstack-control.service"], commands)
        self.assertIn(["systemctl", "--no-block", "try-restart",
                       "devstack-public.service"], commands)
        self.assertIn(["systemctl", "--no-block", "start",
                       "devstack-network-reconcile.timer"], commands)
        self.assertTrue(all(len(args) >= 2 and args[1] == "--no-block"
                            for args in commands))
        self.assertTrue(all(kwargs["timeout"] <= 8
                            for label, _, kwargs in operations if label == "systemctl"))

if __name__ == "__main__":
    unittest.main()
