"""Tailscale preview setup is explicit and must preserve private dashboard."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

from scripts import private_access as pa


@contextmanager
def setup_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "enable-previews.py"
    spec = importlib.util.spec_from_file_location("enable_previews", script)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"private_access": pa}):
        spec.loader.exec_module(module)
    yield module


def state(preview=False, funnel=False):
    config = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            "devstack.example.ts.net:443": {
                "Handlers": {"/": {"Proxy": pa.LOCAL_TARGET}}
            }
        },
        "AllowFunnel": {},
    }
    if preview:
        config["TCP"]["8443"] = {"HTTPS": True}
        config["Web"]["devstack.example.ts.net:8443"] = {
            "Handlers": {"/": {"Proxy": pa.PREVIEW_TARGET}}
        }
    if funnel:
        config["AllowFunnel"]["devstack.example.ts.net:8443"] = True
    return json.dumps(config)


class SetupTests(unittest.TestCase):
    @patch("scripts.private_access.run_cli")
    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.connected_hostname", return_value="devstack.example.ts.net")
    def test_existing_preview_route_is_idempotent(self, hostname, pin, cli):
        cli.side_effect = ["{}", state(preview=True)]
        with setup_module() as module:
            self.assertIn("already available", module.enable())
        self.assertEqual(cli.call_count, 2)

    @patch("scripts.private_access.run_cli")
    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.connected_hostname", return_value="devstack.example.ts.net")
    def test_public_funnel_is_rejected_without_changes(self, hostname, pin, cli):
        cli.side_effect = ["{}", state(preview=True, funnel=True)]
        with setup_module() as module:
            with self.assertRaises(pa.PrivateAccessError):
                module.enable()
        self.assertEqual(cli.call_count, 2)

    @patch("scripts.private_access.run_cli")
    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.connected_hostname", return_value="devstack.example.ts.net")
    @patch("urllib.request.urlopen")
    def test_adds_only_separate_8443_serve_mapping(self, urlopen, hostname, pin, cli):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = b'{"scope":"previews-only"}'
        cli.side_effect = ["{}", state(preview=False), "", state(preview=True)]
        with setup_module() as module:
            self.assertIn("8443", module.enable())
        self.assertEqual(cli.call_args_list[2].args, (
            "serve", "--bg", "--https=8443", pa.PREVIEW_TARGET,
        ))
        self.assertEqual(cli.call_count, 4)


if __name__ == "__main__":
    unittest.main()
