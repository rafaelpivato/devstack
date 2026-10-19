"""ADR-0001 webhook isolation and in-flight generation lease regression tests."""
from http.client import HTTPConnection
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from devstack.control.daemon import Controller
from devstack.control.ingress import (
    WebhookServer, WebhookNotFound, WebhookInactive, WebhookBusy, WebhookInvalid,
)
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database


class FakeDocker:
    def __init__(self):
        self.containers = []
        self.events = []

    def owned(self):
        return list(self.containers)

    def validate(self, identifier):
        self.events.append(("validate", identifier))

    def start(self, identifier, generation):
        self.events.append(("start", identifier))
        self.containers = [{
            "id": str(uuid4()), "project": identifier,
            "generation": generation, "state": "running",
        }]

    def verify(self, identifier, generation):
        assert self.containers and all(
            record["project"] == identifier and record["generation"] == generation
            for record in self.containers
        )

    def stop(self, identifier):
        self.events.append(("stop", identifier))
        self.containers = [
            item for item in self.containers if item["project"] != identifier
        ]

    def webhook_target(self, identifier, generation, service):
        assert self.containers[0]["project"] == identifier
        assert self.containers[0]["generation"] == generation
        assert service == "web"
        return "10.11.12.13"


class Response:
    status = 201
    headers = {"Content-Type": "text/plain"}
    def read(self, n):
        return b"accepted"
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass


class WebhookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.backend = FakeDocker()
        self.controller = Controller(
            Database(base / "state" / "control.sqlite3"),
            WorkspaceStore(base / "projects"), self.backend,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "Alpha")
        self.b = self.controller.provisioner.create_empty("beta", "Beta")
        self.ingress = self.controller.ingress
        for project in (self.a, self.b):
            self.ingress.configure(project.id, "stripe", "web", 8000, "/hooks/stripe")

    def test_same_webhook_route_is_separate_per_project(self):
        self.assertEqual(len(self.ingress.list(self.a.id)), 1)
        self.assertEqual(len(self.ingress.list(self.b.id)), 1)
        self.controller.coordinator.activate(self.a.id)
        with self.assertRaises(WebhookInactive):
            self.ingress.forward(self.b.id, "stripe", b"{}", {})
        with self.assertRaises(WebhookNotFound):
            self.ingress.forward(self.a.id, "unknown", b"{}", {})
        forwarded = []
        def fake_open(req, timeout):
            forwarded.append(req)
            return Response()
        with patch("devstack.control.ingress.urlopen", fake_open):
            status, body, kind = self.ingress.forward(
                self.a.id, "stripe", b'{"event":1}',
                {"Stripe-Signature": "test-signature", "Content-Type": "application/json"},
            )
        self.assertEqual((status, body, kind), (201, b"accepted", "text/plain"))
        self.assertEqual(forwarded[0].data, b'{"event":1}')
        self.assertEqual(forwarded[0].get_header("Stripe-signature"), "test-signature")
        self.assertEqual(forwarded[0].full_url, "http://10.11.12.13:8000/hooks/stripe")
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.a.id)

    def test_switch_waits_for_inflight_webhook_lease(self):
        self.controller.coordinator.activate(self.a.id)
        waiting = threading.Event()
        release = threading.Event()
        results = []
        def slow_open(_req, timeout):
            waiting.set()
            release.wait(timeout=3)
            return Response()
        def forward():
            try:
                results.append(self.ingress.forward(self.a.id, "stripe", b"body", {}))
            except Exception as exc:
                results.append(exc)
        with patch("devstack.control.ingress.urlopen", slow_open):
            sender = threading.Thread(target=forward)
            sender.start()
            self.assertTrue(waiting.wait(2))
            switch_result = []
            switch_thread = threading.Thread(
                target=lambda: switch_result.append(
                    self.controller.coordinator.switch(self.b.id)
                )
            )
            switch_thread.start()
            time.sleep(.1)
            self.assertNotIn(("start", self.b.id), self.backend.events)
            with self.controller.registry.database.transaction() as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM webhook_leases"
                ).fetchone()[0], 1)
            release.set()
            sender.join(5)
            switch_thread.join(5)
            self.assertFalse(sender.is_alive())
            self.assertFalse(switch_thread.is_alive())
        self.assertEqual(results[0][0], 201)
        self.assertEqual(switch_result[0]["project_id"], self.b.id)
        self.assertEqual(self.backend.events[-2:], [
            ("stop", self.a.id), ("start", self.b.id),
        ])
        with self.controller.registry.database.transaction() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM webhook_leases"
            ).fetchone()[0], 0)

    def test_failed_drain_blocks_switch_and_leaves_error(self):
        self.controller.coordinator.activate(self.a.id)
        with patch.object(self.ingress, "drain",
                          side_effect=WebhookBusy("lease not drained")):
            with self.assertRaisesRegex(Exception, "failed closed"):
                self.controller.coordinator.switch(self.b.id)
        self.assertEqual(self.controller.coordinator.status()["phase"], "error")
        self.assertNotIn(("start", self.b.id), self.backend.events)

    def test_routing_validation_and_deletion_are_project_scoped(self):
        for route, service, port, path in (
            ("../x", "web", 8000, "/ok"),
            ("hook", "../../web", 8000, "/ok"),
            ("hook", "web", 0, "/ok"),
            ("hook", "web", 8000, "//evil.com"),
            ("hook", "web", 8000, "/foo/../bar"),
        ):
            with self.subTest(route=route, service=service), self.assertRaises(WebhookInvalid):
                self.ingress.configure(self.a.id, route, service, port, path)
        self.ingress.remove(self.a.id, "stripe")
        self.assertEqual(self.ingress.list(self.a.id), ())
        self.assertEqual(len(self.ingress.list(self.b.id)), 1)

    def test_public_listener_is_loopback_and_only_accepts_known_post_paths(self):
        self.controller.coordinator.activate(self.a.id)
        with WebhookServer(self.ingress, bind_port=0) as server:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                port = server.server_address[1]
                def post(path):
                    conn = HTTPConnection("127.0.0.1", port, timeout=4)
                    try:
                        conn.request("POST", path, b"{}", headers={
                            "Content-Type": "application/json"
                        })
                        response = conn.getresponse()
                        return response.status, response.read()
                    finally:
                        conn.close()
                self.assertEqual(post("/hooks/" + self.b.id + "/stripe")[0], 503)
                self.assertEqual(post("/hooks/" + self.a.id + "/nonexistent")[0], 404)
                self.assertEqual(post("/bad")[0], 404)
                with patch("devstack.control.ingress.urlopen", return_value=Response()):
                    self.assertEqual(post("/hooks/" + self.a.id + "/stripe")[0], 201)
            finally:
                server.shutdown()
                worker.join(4)


if __name__ == "__main__":
    unittest.main()
