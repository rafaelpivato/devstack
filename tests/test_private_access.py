import unittest
from unittest.mock import patch

from scripts.private_access import (
    LOCAL_TARGET, PrivateAccessError, connected_hostname, configure,
    is_empty_serve, is_our_private_serve, parse_json,
)


def example_serve(target=LOCAL_TARGET, funnel=False):
    return {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            "devstack.example.ts.net:443": {
                "Handlers": {"/": {"Proxy": target}}
            }
        },
        "AllowFunnel": {"devstack.example.ts.net:443": funnel},
    }


def connected():
    return '{"BackendState":"Running","Self":{"DNSName":"devstack.example.ts.net."}}'


class PrivateAccessTests(unittest.TestCase):
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
        self.assertEqual(url, "Private dashboard: https://devstack.example.ts.net/")
        self.assertEqual(cli.call_args_list[-1].args, (
            "serve", "--bg", "--https=443", LOCAL_TARGET
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


if __name__ == "__main__":
    unittest.main()
