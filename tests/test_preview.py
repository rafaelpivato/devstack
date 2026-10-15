"""Private preview gateway must never proxy arbitrary hostnames/containers."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from devstack.applications import Application
from devstack.preview import (
    PreviewError, create_preview_app, _container, _target,
)
from devstack.core import Project


class OriginHandler(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        self.__class__.seen.append({
            "path": self.path, "cookie": self.headers.get("Cookie"),
            "authorization": self.headers.get("Authorization"),
            "forwarded": self.headers.get("X-Forwarded-Prefix"),
            "host": self.headers.get("Host"),
        })
        if self.path.endswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/health")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Set-Cookie", "bad=upstream; Path=/")
            self.send_header("Cache-Control", "public,max-age=100")
            self.end_headers()
            self.wfile.write(("upstream:" + self.path).encode("utf-8"))

    def log_message(self, *args):
        pass


class PreviewProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
        cls.server = threading.Thread(target=cls.origin.serve_forever, daemon=True)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.origin.shutdown()
        cls.origin.server_close()
        cls.server.join(timeout=3)

    def setUp(self):
        OriginHandler.seen.clear()
        self.app = create_preview_app({"TESTING": True})
        self.client = self.app.test_client()
        self.p1 = patch("devstack.preview.load_projects", return_value={
            "smoke": Project("smoke", (), ())
        })
        self.p2 = patch("devstack.preview.load_applications", return_value=(
            Application("Web", "web", 8000),
        ))
        self.p3 = patch("devstack.preview._target", return_value=(
            f"http://127.0.0.1:{self.origin.server_port}"
        ))
        for p in (self.p1, self.p2, self.p3):
            p.start()
            self.addCleanup(p.stop)

    def test_health_is_not_admin_health(self):
        result = self.client.get("/healthz")
        self.assertEqual(result.json, {"status": "ok", "scope": "previews-only"})
        self.assertEqual(self.client.get("/").json["scope"], "private application previews")

    def test_unregistered_or_portless_services_are_not_proxied(self):
        self.assertEqual(self.client.get("/p/unknown/Web/").status_code, 404)
        self.assertEqual(self.client.get("/p/smoke/Redis/").status_code, 404)
        with patch("devstack.preview.load_applications", return_value=(
            Application("Web", "web", None),)):
            self.assertEqual(self.client.get("/p/smoke/Web/").status_code, 404)
        self.assertEqual(OriginHandler.seen, [])

    def test_proxy_path_queries_no_cookies_or_auth_headers(self):
        result = self.client.get(
            "/p/smoke/Web/health?debug=true",
            headers={
                "Cookie": "admin=private",
                "Authorization": "Bearer secret",
                "X-Forwarded-For": "1.2.3.4",
            }
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data, b"upstream:/health?debug=true")
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", result.headers)
        self.assertEqual(result.headers["X-Frame-Options"], "DENY")
        seen = OriginHandler.seen[-1]
        self.assertIsNone(seen["cookie"])
        self.assertIsNone(seen["authorization"])
        self.assertEqual(seen["forwarded"], "/p/smoke/Web")
        self.assertEqual(seen["host"], f"127.0.0.1:{self.origin.server_port}")

    def test_redirect_stays_inside_application_prefix(self):
        result = self.client.get("/p/smoke/Web/redirect")
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result.headers["Location"], "/p/smoke/Web/health")

    def test_reject_body_over_limit(self):
        result = self.client.post(
            "/p/smoke/Web/upload", data=b"x" * (5 * 1024 * 1024 + 1)
        )
        self.assertEqual(result.status_code, 413)


class PreviewTargetTests(unittest.TestCase):
    def setUp(self):
        self.owner = "devstack-demo-deadbeef01"
        self.cid = "a" * 64
        self.netid = "b" * 64
        self.container = {
            "Config": {"Labels": {
                "com.docker.compose.project": self.owner,
                "com.docker.compose.service": "web",
            }},
            "State": {"Running": True},
            "NetworkSettings": {"Networks": {
                "default": {"IPAddress": "172.19.0.5", "NetworkID": self.netid}
            }},
        }
        self.network = [{
            "Id": self.netid, "Labels": {
                "com.docker.compose.project": self.owner,
                "com.docker.compose.network": "default",
            }
        }]

    @patch("devstack.preview.compose_name")
    @patch("devstack.preview._container")
    @patch("devstack.preview._json_run")
    def test_only_compose_default_network_is_accepted(self, docker, container, pname):
        pname.return_value = self.owner
        container.return_value = self.container
        docker.return_value = self.network
        self.assertEqual(_target("demo", "web", 8000), "http://172.19.0.5:8000")
        self.assertIn(self.netid, docker.call_args.args[0])

    @patch("devstack.preview.compose_name")
    @patch("devstack.preview._container")
    @patch("devstack.preview._json_run")
    def test_reject_foreign_network(self, docker, container, pname):
        pname.return_value = self.owner
        container.return_value = self.container
        docker.return_value = [{
            "Id": self.netid, "Labels": {
                "com.docker.compose.project": "other",
                "com.docker.compose.network": "default",
            }
        }]
        with self.assertRaisesRegex(PreviewError, "default Compose network"):
            _target("demo", "web", 8000)
        self.container["NetworkSettings"]["Networks"]["default"]["IPAddress"] = "127.0.0.1"
        docker.return_value = self.network
        with self.assertRaises(PreviewError):
            _target("demo", "web", 8000)

    @patch("devstack.preview.compose_name", return_value="devstack-demo-deadbeef01")
    @patch("devstack.preview.compose_command", return_value=["docker", "compose"])
    @patch("devstack.preview.compose_path")
    @patch("devstack.preview.project_root", return_value=Path("/tmp/demo"))
    @patch("devstack.preview.subprocess.run")
    @patch("devstack.preview._json_run")
    def test_container_labels_must_match_exactly(self, inspect, run, root, file, command, name):
        file.return_value.is_file.return_value = True
        run.return_value.returncode = 0
        run.return_value.stdout = self.cid + "\n"
        inspect.return_value = [self.container]
        self.assertEqual(_container("demo", "web")["State"]["Running"], True)
        self.container["Config"]["Labels"]["com.docker.compose.service"] = "database"
        with self.assertRaisesRegex(PreviewError, "does not belong"):
            _container("demo", "web")


if __name__ == "__main__":
    unittest.main()
