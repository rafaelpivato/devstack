"""Direct tailnet development URLs without an unnecessary reverse proxy."""
import json
import subprocess
import unittest
from unittest.mock import patch

from devstack.applications import Application
from devstack.dev_ports import tailscale_ipv4, application_urls, published_ports


class DevPortsTests(unittest.TestCase):
    @patch("devstack.dev_ports.subprocess.run")
    def test_only_tailscale_cgnat_ip_is_used(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "100.101.102.103\n", "")
        self.assertEqual(tailscale_ipv4(), "100.101.102.103")
        for invalid in ("192.168.1.2", "127.0.0.1", "8.8.8.8", "bad", "100.101.102.103\n100.101.102.104"):
            with self.subTest(invalid=invalid):
                run.return_value.stdout = invalid
                self.assertIsNone(tailscale_ipv4())

    @patch("devstack.dev_ports.runtime.is_active", return_value=True)
    @patch("devstack.dev_ports.tailscale_ipv4", return_value="100.101.102.103")
    @patch("devstack.dev_ports.compose_path")
    @patch("devstack.dev_ports.compose_command", return_value=["docker", "compose"])
    @patch("devstack.dev_ports.project_root")
    @patch("devstack.dev_ports.subprocess.run")
    def test_only_published_on_tailscale_ip_and_matching_registered_port(
            self, run, root, command, compose, ts, active):
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

    @patch("devstack.dev_ports.runtime.is_active", return_value=False)
    @patch("devstack.dev_ports.tailscale_ipv4")
    @patch("devstack.dev_ports.subprocess.run")
    def test_inactive_runtime_does_not_discover_ports(self, run, ts, active):
        self.assertEqual(published_ports("demo"), ())
        run.assert_not_called()
        ts.assert_not_called()

    @patch("devstack.dev_ports.runtime.is_active", return_value=True)
    @patch("devstack.dev_ports.tailscale_ipv4", return_value="100.101.102.103")
    @patch("devstack.dev_ports.compose_path")
    @patch("devstack.dev_ports.compose_command", return_value=["docker", "compose"])
    @patch("devstack.dev_ports.project_root")
    @patch("devstack.dev_ports.subprocess.run")
    def test_dynamic_compose_ports_and_optional_app_port(self, run, root, command, compose, ts, active):
        compose.return_value.is_file.return_value = True
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps([
            {"Service": "api", "State": "running", "Publishers": [
                {"URL": "100.101.102.103", "TargetPort": 8000, "PublishedPort": 40004,
                 "Protocol": "tcp"},
                {"URL": "100.101.102.103", "TargetPort": 9000, "PublishedPort": 40005,
                 "Protocol": "udp"},
                {"URL": "0.0.0.0", "TargetPort": 9000, "PublishedPort": 9000,
                 "Protocol": "tcp"},
            ]},
            {"Service": "web", "State": "running", "Publishers": [
                {"URL": "100.101.102.103", "TargetPort": 5173, "PublishedPort": 5173,
                 "Protocol": "tcp"}
            ]},
        ]), "")
        mappings = published_ports("demo")
        self.assertEqual([(p.service, p.container_port, p.host_port) for p in mappings],
                         [("api", 8000, 40004), ("web", 5173, 5173)])
        self.assertEqual(application_urls("demo", (
            Application("API", "api", None),
            Application("Web", "web", 5173),
        )), {"API": "http://100.101.102.103:40004/",
             "Web": "http://100.101.102.103:5173/"})

    @patch("devstack.dev_ports.tailscale_ipv4", return_value=None)
    @patch("devstack.dev_ports.subprocess.run")
    def test_no_tailnet_returns_no_links(self, run, ts):
        self.assertEqual(application_urls("demo", (Application("Web", "web", 8000),)), {})
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
