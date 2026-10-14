"""Temporary first-use HTTP portal: narrow auth and URL extraction."""
import hashlib
from http.client import HTTPConnection
import os
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from scripts.bootstrap import Handler, extract_login_url, valid_setup_secret


class BootstrapTests(unittest.TestCase):
    def test_extract_login_url_from_tailscale_json(self):
        output = (
            '{"AuthURL":"https://login.tailscale.com/a/abcDEF123",'
            '"BackendState":"NeedsLogin"}\n'
            '{"BackendState":"Running"}\n'
        )
        self.assertEqual(
            extract_login_url(output),
            "https://login.tailscale.com/a/abcDEF123",
        )

    def test_rejects_non_tailscale_url(self):
        with self.assertRaises(ValueError):
            extract_login_url("https://attacker.example/a/bad")

    def test_secret_is_hash_checked(self):
        digest = hashlib.sha256(b"totally-random-secret-value").hexdigest()
        self.assertTrue(valid_setup_secret("totally-random-secret-value", digest))
        self.assertFalse(valid_setup_secret("wrong", digest))

    def test_http_post_requires_secret_before_disclosing_login(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        secret = "very-long-test-bootstrap-password"
        digest = hashlib.sha256(secret.encode()).hexdigest()
        try:
            with patch.dict(os.environ, {"DEVSTACK_BOOTSTRAP_HASH": digest}):
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request("GET", "/")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertNotIn(b"login.tailscale.com/a/", response.read())
                conn.close()

                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                payload = urlencode({"secret": "wrong"})
                conn.request("POST", "/activate", payload,
                             {"Content-Type": "application/x-www-form-urlencoded"})
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                conn.close()

                with patch("scripts.bootstrap.URL_FILE", Path("/does/not/exist")):
                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    payload = urlencode({"secret": secret})
                    conn.request("POST", "/activate", payload,
                                 {"Content-Type": "application/x-www-form-urlencoded"})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 503)
                    conn.close()

                import tempfile
                with tempfile.TemporaryDirectory() as folder:
                    url = Path(folder) / "activation-url"
                    url.write_text('{"AuthURL":"https://login.tailscale.com/a/abc123"}')
                    with patch("scripts.bootstrap.URL_FILE", url):
                        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                        payload = urlencode({"secret": secret})
                        conn.request("POST", "/activate", payload,
                                     {"Content-Type": "application/x-www-form-urlencoded"})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 303)
                        self.assertEqual(
                            response.getheader("Location"),
                            "https://login.tailscale.com/a/abc123",
                        )
                        conn.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
