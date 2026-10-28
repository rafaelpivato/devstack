"""Validate that trusted v0.7.3 postinstall runs networking for old updaters."""
import unittest
from unittest.mock import patch
from devstack.cli import install as cli

class UpgradeTests(unittest.TestCase):
    def test_older_updater_can_trigger_auto_network_reconcile(self):
        events=[]
        def record(name):
            def callback(*args, **kwargs):
                events.append(name)
                return False
            return callback
        with (patch.object(cli.os,"geteuid",return_value=0),
              patch.object(cli,"install_command",side_effect=record("cli")),
              patch.object(cli,"refresh_updater",side_effect=record("refresh")),
              patch("devstack.networking.prepare",side_effect=record("prepare")),
              patch("devstack.mcp_management.install_units",side_effect=record("units")),
              patch.object(cli.subprocess,"run",side_effect=record("systemctl"))):
            self.assertEqual(cli.main([]),0)
        self.assertLess(events.index("refresh"),events.index("prepare"))
        self.assertNotIn("reconcile", events)
        self.assertIn("systemctl", events)
