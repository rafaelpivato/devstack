"""Public webhook ingress: explicit routes, POST-only, raw body and signatures."""
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from devstack.applications import Application
from devstack.core import Project
from devstack.webhooks import (
    WebhookError, Webhook, add_webhook, remove_webhook,
    load_webhooks, create_webhook_app, _target,
)


class TargetHandler(BaseHTTPRequestHandler):
    captured = []

    def do_POST(self):
        count = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(count)
        self.__class__.captured.append((self.path, body, dict(self.headers)))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Set-Cookie", "do-not-forward=1")
        self.end_headers()
        self.wfile.write(b"delivered")

    def log_message(self, *args):
        pass


class WebhookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.listener = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        cls.thread = threading.Thread(target=cls.listener.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / "workspaces" / "demo" / ".devstack").mkdir(parents=True)
        # Compose helpers use the server user home directory.
        patches = [
            patch("devstack.docker_stacks.Path.home", return_value=self.home),
            patch("devstack.webhooks.load_projects", return_value={
                "demo": Project("demo", (), ())
            }),
            patch("devstack.docker_stacks.load_projects", return_value={
                "demo": Project("demo", (), ())
            }),
            patch("devstack.webhooks.load_applications", return_value=(
                Application("API", "backend", 8000),
            )),
            patch("devstack.webhooks._target", return_value=(
                f"http://127.0.0.1:{self.listener.server_port}"
            )),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        TargetHandler.captured.clear()

    def test_explicit_route_round_trip_and_delete(self):
        add_webhook("demo", "stripe", "API", "/webhooks/stripe")
        self.assertEqual(load_webhooks("demo"), (
            Webhook("stripe", "API", "/webhooks/stripe"),
        ))
        with self.assertRaisesRegex(WebhookError, "already exists"):
            add_webhook("demo", "stripe", "API", "/webhooks/stripe")
        remove_webhook("demo", "stripe")
        self.assertEqual(load_webhooks("demo"), ())

    def test_reject_invalid_paths_or_targets(self):
        for target in ("http://evil", "//evil", "/a/../secret",
                       "/api?redirect=1", "/api%2fprivate"):
            with self.subTest(target=target), self.assertRaises(WebhookError):
                add_webhook("demo", "invalid", "API", target)
        with self.assertRaises(WebhookError):
            add_webhook("demo", "no-app", "Redis", "/hooks/db")

    def test_exact_post_only_no_other_paths(self):
        app = create_webhook_app({"TESTING": True})
        c = app.test_client()
        self.assertEqual(c.get("/").status_code, 404)
        self.assertEqual(c.get("/hooks/demo/stripe").status_code, 405)
        self.assertEqual(c.post("/hooks/demo/stripe", data=b"").status_code, 404)
        add_webhook("demo", "stripe", "API", "/webhooks/stripe")
        self.assertEqual(c.post("/hooks/demo/other", data=b"").status_code, 404)
        self.assertEqual(c.post("/p/demo/stripe/", data=b"").status_code, 404)
        self.assertEqual(c.get("/healthz").json["scope"], "webhook-only")

    def test_provider_signature_and_raw_json_are_forwarded(self):
        add_webhook("demo", "stripe", "API", "/webhooks/stripe")
        payload = b'{"id":"evt_123","raw": true}'
        c = create_webhook_app({"TESTING": True}).test_client()
        result = c.post("/hooks/demo/stripe", data=payload,
                        headers={"Stripe-Signature": "t=100,v1=abcd",
                                 "Authorization": "provider-custom",
                                 "Content-Type": "application/json",
                                 "Cookie": "admin=secret",
                                 "X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data, b"delivered")
        self.assertNotIn("Set-Cookie", result.headers)
        self.assertEqual(len(TargetHandler.captured), 1)
        path, body, headers = TargetHandler.captured[0]
        self.assertEqual(path, "/webhooks/stripe")
        self.assertEqual(body, payload)
        self.assertEqual(headers["Stripe-Signature"], "t=100,v1=abcd")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("X-Forwarded-For", headers)

    def test_payload_size_limit(self):
        add_webhook("demo", "stripe", "API", "/webhooks/stripe")
        c = create_webhook_app({"TESTING": True}).test_client()
        result = c.post("/hooks/demo/stripe", data=b"x" * (2 * 1024 * 1024 + 1))
        self.assertEqual(result.status_code, 413)


if __name__ == "__main__":
    unittest.main()
