"""Direct tailnet development URLs without an unnecessary reverse proxy."""
import json
import subprocess
import unittest
from unittest.mock import patch

from devstack.applications import Application
from devstack.dev_ports import tailscale_ipv4, application_urls


class DevPortsTests(unittest.TestCase):
    @patch("devstack.dev_ports.subprocess.run")
    def test_only_tailscale_cgnat_ip_is_used(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "100.101.102.103\n", "")
        self.assertEqual(tailscale_ipv4(), "100.101.102.103")
        for invalid in ("192.168.1.2", "127.0.0.1", "8.8.8.8", "bad", "100.101.102.103\n100.101.102.104"):
            with self.subTest(invalid=invalid):
                run.return_value.stdout = invalid
                self.assertIsNone(tailscale_ipv4())

    @patch("devstack.dev_ports.tailscale_ipv4", return_value="100.101.102.103")
    @patch("devstack.dev_ports.compose_path")
    @patch("devstack.dev_ports.compose_command", return_value=["docker", "compose"])
    @patch("devstack.dev_ports.project_root")
    @patch("devstack.dev_ports.subprocess.run")
    def test_only_published_on_tailscale_ip_and_matching_registered_port(
            self, run, root, command, compose, ts):
        compose.return_value.is_file.return_value = True
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps([
            {"Service": "web", "State": "running", "Publishers": [
                {"URL": "100.101.102.103", "TargetPort": 8000,
                 "PublishedPort": 18080, "Protocol": "tcp"},
                {"URL": "0.0.0.0", "TargetPort": 8000,
                 "PublishedPort": 8000, "Protocol": "tcp"},
            ]},
            {"Service": "redis", "State": "running", "Publishers": [
                {"URL": "100.101.102.103", "TargetPort": 6379,
                 "PublishedPort": 6379, "Protocol": "tcp"},
            ]},
            {"Service": "sleep", "State": "stopped", "Publishers": [
                {"URL": "100.101.102.103", "TargetPort": 3333,
                 "PublishedPort": 3333, "Protocol": "tcp"}
            ]},
        ])
        result = application_urls("demo", (
            Application("Frontend", "web", 8000),
            Application("Different port", "web", 5173),
        ))
        self.assertEqual(result, {"Frontend": "http://100.101.102.103:18080/"})
        self.assertEqual(run.call_count, 1)

    @patch("devstack.dev_ports.tailscale_ipv4", return_value=None)
    @patch("devstack.dev_ports.subprocess.run")
    def test_no_tailnet_returns_no_links(self, run, ts):
        self.assertEqual(application_urls("demo", (Application("Web", "web", 8000),)), {})
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
