"""Funnel is never enabled by a dashboard action or during installation."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from scripts import private_access as pa


def status(funnel=False):
    result = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {"devstack.example.ts.net:443": {
            "Handlers": {"/": {"Proxy": pa.LOCAL_TARGET}},
        }},
        "AllowFunnel": {},
    }
    if funnel:
        result["TCP"]["8443"] = {"HTTPS": True}
        result["Web"]["devstack.example.ts.net:8443"] = {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}
        }
        result["AllowFunnel"]["devstack.example.ts.net:8443"] = True
    return json.dumps(result)


def connected():
    return json.dumps({
        "BackendState": "Running",
        "Self": {"DNSName": "devstack.example.ts.net.", "ID": "n1"},
        "CurrentTailnet": {"Name": "example.ts.net"},
    })


@contextmanager
def funnel():
    path = Path(__file__).resolve().parents[1] / "scripts" / "webhook_funnel.py"
    spec = importlib.util.spec_from_file_location("webhook_funnel_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"private_access": pa}):
        spec.loader.exec_module(module)
    yield module


class FunnelTests(unittest.TestCase):
    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.run_cli")
    def test_already_enabled_does_not_modify_routes(self, cli, pin):
        cli.side_effect = [connected(), status(funnel=True)]
        with funnel() as helper:
            self.assertIn("Webhooks public", helper.configure())
        self.assertEqual(cli.call_count, 2)

    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.run_cli")
    def test_disabling_funnel_leaves_private_dashboard(self, cli, pin):
        cli.side_effect = [connected(), status(funnel=True), "", status()]
        with funnel() as helper:
            self.assertIn("disabled", helper.configure(disable=True))
        self.assertEqual(cli.call_args_list[2].args,
                         ("funnel", "--https=8443", "off"))

    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.run_cli")
    def test_refuses_foreign_or_public_existing_serve(self, cli, pin):
        cli.side_effect = [connected(), '{"TCP":{"443":{"HTTPS":true}},"Web":{}}']
        with funnel() as helper, self.assertRaises(pa.PrivateAccessError):
            helper.configure()
        self.assertEqual(cli.call_count, 2)

    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.run_cli")
    @patch("urllib.request.urlopen")
    def test_enables_exact_8443_for_webhook_only_listener(self, urlopen, cli, pin):
        urlopen.return_value.__enter__.return_value.read.return_value = b'{"scope":"webhook-only"}'
        cli.side_effect = [connected(), status(), "", status(funnel=True)]
        with funnel() as helper:
            self.assertIn(":8443/hooks/", helper.configure())
        self.assertEqual(cli.call_args_list[2].args,
                         ("funnel", "--yes", "--bg", "--https=8443",
                          "http://127.0.0.1:8767"))
        self.assertEqual(cli.call_count, 4)


if __name__ == "__main__":
    unittest.main()
