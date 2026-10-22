"""v0.6.2 secure DevStack-wide diagnostics and read-only controller IPC."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack.api.http import create_app
from devstack.diagnostics import DiagnosticStore, MAX_EVENTS
from devstack.ipc.protocol import ProtocolError, validate_request
from devstack.ipc.client import ControllerUnavailable


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DiagnosticStore(Path(self.tmp.name) / "events.sqlite3")

    def test_allowlisted_events_only_no_arbitrary_logs(self):
        self.db.record("controller", "controller-started")
        self.assertEqual(self.db.snapshot(5)["events"][0]["code"], "controller-started")
        for params in (("controller", "Bearer secret", "info"),
                       ("docker", "controller-started", "info"),
                       ("web", "web-error", "raw-data")):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.db.record(*params)
        self.assertNotIn("secret", str(self.db.snapshot(5)))
        self.assertEqual(self.db.path.stat().st_mode & 0o777, 0o600)

    def test_bounded_windows_and_retention(self):
        for minutes in (1, 5, 30):
            self.assertEqual(self.db.snapshot(minutes)["events"], [])
        with self.assertRaises(ValueError):
            self.db.snapshot(31)
        with self.assertRaises(ValueError):
            self.db.snapshot("5")
        for _ in range(MAX_EVENTS + 2):
            self.db.record("web", "web-error", "warning")
        snapshot = self.db.snapshot(30)
        self.assertEqual(len(snapshot["events"]), MAX_EVENTS)
        self.assertTrue(snapshot["truncated"])
        with self.assertRaises(ValueError):
            self.db.record("web", "web-error", "token=hunter2")

    def test_symlink_refused(self):
        symlink = Path(self.tmp.name) / "symlink.sqlite3"
        symlink.symlink_to(self.db.path)
        with self.assertRaises(RuntimeError):
            DiagnosticStore(symlink).snapshot(1)


class DiagnosticHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        def transport(op, **fields):
            self.calls.append((op, fields))
            if op == "diagnostics_snapshot":
                return {
                    "events": [
                        {"at": 2000, "source": "runtime", "level": "error",
                         "code": "transition-failed", "operation_id": "receipt",
                         "action": "deactivate", "error_code": "ControlError"},
                        {"at": 1000, "source": "controller", "level": "info",
                         "code": "controller-started"},
                    ],
                    "sources": {"controller": "available", "runtime": "available"},
                    "truncated": False,
                }
            raise AssertionError("Unexpected controller operation " + op)
        self.app = create_app({
            "TESTING": True, "SECRET_KEY": "test",
            "WEB_DIAGNOSTICS_PATH": str(Path(self.tmp.name) / "web.sqlite3"),
            "SESSION_COOKIE_SECURE": False,
        }, transport=transport)
        self.client = self.app.test_client()

    @patch("devstack.api.http.installed_version", return_value="v0.6.2")
    @patch("devstack.api.http.updater_service_events",
           return_value={"events": [], "available": True})
    @patch("devstack.api.http.update_state",
           return_value={"phase": "idle", "unit_state": "inactive",
                         "last_result": "success", "last_exit_code": "0"})
    def test_only_known_windows_no_mutation_and_order(self, *_):
        self.assertEqual(self.client.get("/__v06/administration/diagnostics").status_code, 200)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.get("/api/v2/devstack/diagnostics?minutes=2").status_code, 400)
        self.assertEqual(self.calls, [])
        resp = self.client.get("/api/v2/devstack/diagnostics?minutes=5")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["minutes"], 5)
        self.assertEqual(data["version"], "v0.6.2")
        self.assertEqual(data["sources"]["updater"], "available")
        self.assertEqual(data["events"][0]["at"], 1000)
        self.assertEqual(data["events"][1]["at"], 2000)
        self.assertEqual(self.calls, [("diagnostics_snapshot", {"minutes": 5})])
        self.assertNotIn("request_key", str(data))
        self.assertEqual(self.client.post("/api/v2/devstack/diagnostics", json={}).status_code, 403)

    @patch("devstack.api.http.installed_version", return_value="v0.6.2")
    @patch("devstack.api.http.updater_service_events",
           return_value={"events": [], "available": False})
    @patch("devstack.api.http.update_state",
           return_value={"phase": "failed", "unit_state": "failed",
                         "last_result": "exit-code", "last_exit_code": "1"})
    def test_controller_down_partial_snapshot_still_readable(self, *_):
        def unavailable(_op, **_fields):
            raise ControllerUnavailable("unavailable")
        self.app = create_app({
            "TESTING": True, "SECRET_KEY": "test",
            "WEB_DIAGNOSTICS_PATH": str(Path(self.tmp.name) / "web.sqlite3"),
        }, transport=unavailable)
        resp = self.app.test_client().get("/api/v2/devstack/diagnostics?minutes=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["sources"]["controller"], "unavailable")
        self.assertEqual(data["sources"]["updater"], "partial (status only)")
        self.assertTrue(data["partial"])
        self.assertEqual(data["updater_status"]["phase"], "failed")

    def test_diagnostics_ipc_rejects_arbitrary_arguments(self):
        self.assertEqual(validate_request({"op":"diagnostics_snapshot","minutes":5}),
                         "diagnostics_snapshot")
        for payload in (
            {"op":"diagnostics_snapshot"},
            {"op":"diagnostics_snapshot","minutes":30,"argv":["sh"]},
            {"op":"diagnostics_snapshot","minutes":5,"path":"/etc/shadow"},
        ):
            with self.assertRaises(ProtocolError):
                validate_request(payload)


if __name__ == "__main__":
    unittest.main()
