import unittest
from unittest.mock import patch

from scripts.private_access import (
    LOCAL_TARGET, PrivateAccessError, connected_hostname, configure,
    is_empty_serve, is_our_private_serve, parse_json,
)


def example_serve(target=LOCAL_TARGET, funnel=False):
    return {
        "TCP": {"8443": {"HTTPS": True}},
        "Web": {
            "devstack.example.ts.net:8443": {
                "Handlers": {"/": {"Proxy": target}}
            }
        },
        "AllowFunnel": {"devstack.example.ts.net:8443": funnel},
    }


def connected():
    return '{"BackendState":"Running","Self":{"DNSName":"devstack.example.ts.net.","ID":"n-devstack"},"CurrentTailnet":{"Name":"example.ts.net"}}'


class PrivateAccessTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "DEVSTACK_TAILSCALE_IDENTITY": self.tmp.name + "/identity.json"
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_authenticated_hostname(self):
        self.assertEqual(
            connected_hostname(parse_json(connected())),
            "devstack.example.ts.net",
        )

    def test_disconnected(self):
        with self.assertRaises(PrivateAccessError):
            connected_hostname({"BackendState": "NeedsLogin"})

    def test_empty_config(self):
        self.assertTrue(is_empty_serve(parse_json("null")))
        self.assertTrue(is_empty_serve({"TCP": {}, "Web": {}}))
        self.assertFalse(is_empty_serve(example_serve()))

    def test_only_private_serve_is_accepted(self):
        self.assertTrue(is_our_private_serve(example_serve()))
        self.assertFalse(is_our_private_serve(example_serve(funnel=True)))
        self.assertFalse(is_our_private_serve(example_serve("http://127.0.0.1:9000")))
        self.assertFalse(is_our_private_serve({"Services": {"svc:other": {}}}))

    @patch("scripts.private_access.run_cli")
    def test_first_time_configures_background_https(self, cli):
        cli.side_effect = [connected(), "{}", ""]
        url = configure()
        self.assertEqual(url, "Private dashboard: https://devstack.example.ts.net:8443/")
        self.assertEqual(cli.call_args_list[-1].args, (
            "serve", "--bg", "--https=8443", LOCAL_TARGET
        ))

    @patch("scripts.private_access.run_cli")
    def test_idempotent_no_changes(self, cli):
        import json
        cli.side_effect = [connected(), json.dumps(example_serve())]
        url = configure()
        self.assertIn("already configured", url)
        self.assertEqual(cli.call_count, 2)

    @patch("scripts.private_access.run_cli")
    def test_refuses_existing_serve(self, cli):
        import json
        cli.side_effect = [connected(), json.dumps(example_serve(funnel=True))]
        with self.assertRaises(PrivateAccessError):
            configure()
        self.assertEqual(cli.call_count, 2)

    def test_bad_json_fails_closed(self):
        with self.assertRaises(PrivateAccessError):
            parse_json("<html>not json</html>")


    @patch("scripts.private_access.configure", return_value="Private dashboard: https://devstack.example.ts.net:8443/")
    @patch("scripts.private_access.run_cli")
    def test_finish_after_browser_approval(self, cli, configure):
        from scripts.private_access import finish_if_connected
        cli.return_value = connected()
        self.assertIn("Private dashboard:", finish_if_connected())
        configure.assert_called_once_with()

    @patch("scripts.private_access.configure")
    @patch("scripts.private_access.run_cli")
    def test_finish_waits_without_reenrolling(self, cli, configure):
        from scripts.private_access import finish_if_connected
        cli.return_value = '{"BackendState":"NeedsLogin"}'
        self.assertIn("Awaiting", finish_if_connected())
        configure.assert_not_called()


class IdentityPinTests(unittest.TestCase):
    def test_pins_first_identity_and_refuses_reenrollment(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from scripts.private_access import pin_initial_identity
        import os

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "identity.json"
            status = json.loads(connected())
            with patch.dict(os.environ, {"DEVSTACK_TAILSCALE_IDENTITY": str(target)}):
                pin_initial_identity(status)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                pin_initial_identity(status)
                status["CurrentTailnet"]["Name"] = "other.ts.net"
                with self.assertRaisesRegex(PrivateAccessError, "changed"):
                    pin_initial_identity(status)

    def test_incomplete_identity_fails_closed(self):
        import tempfile
        import os
        from unittest.mock import patch
        from scripts.private_access import pin_initial_identity
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {
                "DEVSTACK_TAILSCALE_IDENTITY": directory + "/pin.json"
            }):
                with self.assertRaises(PrivateAccessError):
                    pin_initial_identity({"Self": {"DNSName": "test.ts.net"}})


if __name__ == "__main__":
    unittest.main()
