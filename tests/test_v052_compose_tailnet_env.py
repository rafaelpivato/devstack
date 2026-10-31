"""Controller Compose bindings must use the live, verified tailnet IP."""
import os
import subprocess
import unittest
from unittest.mock import patch

from devstack.control.docker import _execute


class ComposeTailnetBindingTests(unittest.TestCase):
    def _run(self, argv, tailnet_ip, inherited=None):
        seen = {}

        def fake_run(command, **kwargs):
            seen.update({"argv": command, **kwargs})
            return subprocess.CompletedProcess(command, 0, "OK\n", "")

        env = {"DEVSTACK_TAILSCALE_IPV4": "0.0.0.0"}
        if inherited:
            env.update(inherited)
        with patch.dict(os.environ, env, clear=True), \
             patch("devstack.control.private_ports.tailscale_ipv4",
                   return_value=tailnet_ip) as lookup, \
             patch("devstack.control.docker.subprocess.run",
                   side_effect=fake_run):
            result = _execute(argv, 29)
        self.assertEqual(result, "OK")
        return seen, lookup

    def test_compose_validate_receives_verified_host_and_overrides_inherited(self):
        argv = ["docker", "compose", "--project-directory", "/tmp/source",
                "-f", "/tmp/managed.yaml", "config", "--quiet"]
        seen, lookup = self._run(argv, "100.98.76.54")
        lookup.assert_called_once_with()
        self.assertEqual(seen["env"]["DEVSTACK_TAILSCALE_IPV4"],
                         "100.98.76.54")
        self.assertEqual(seen["argv"], argv)
        self.assertEqual(seen["timeout"], 29)
        self.assertNotEqual(os.environ.get("DEVSTACK_TAILSCALE_IPV4"),
                            "100.98.76.54")

    def test_compose_start_uses_same_portable_binding(self):
        for argv in (
            ["docker", "compose", "up", "-d", "--wait"],
            ["docker", "compose", "down", "--remove-orphans"],
            ["docker", "compose", "config", "--services"],
        ):
            with self.subTest(argv=argv):
                seen, lookup = self._run(argv, "100.70.80.90")
                self.assertEqual(seen["env"]["DEVSTACK_TAILSCALE_IPV4"],
                                 "100.70.80.90")
                lookup.assert_called_once_with()

    def test_invalid_or_missing_tailnet_fails_closed_to_loopback(self):
        for argv in (["docker", "compose", "config", "--quiet"],
                     ["docker", "compose", "up", "-d"]):
            with self.subTest(argv=argv):
                seen, _ = self._run(argv, None)
                self.assertEqual(seen["env"]["DEVSTACK_TAILSCALE_IPV4"],
                                 "127.0.0.1")

    def test_non_compose_docker_command_does_not_get_reserved_env(self):
        argv = ["docker", "inspect", "abc"]
        seen, lookup = self._run(argv, "100.98.76.54")
        lookup.assert_not_called()
        self.assertNotIn("env", seen)

    def test_arbitrary_command_does_not_get_reserved_env(self):
        argv = ["python", "-V"]
        seen, lookup = self._run(argv, "100.98.76.54")
        lookup.assert_not_called()
        self.assertNotIn("env", seen)


if __name__ == "__main__":
    unittest.main()
