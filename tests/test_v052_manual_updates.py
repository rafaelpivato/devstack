"""v0.5.2 manual updates: CSRF, least privilege, safe release-only requests."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack.api.client import ControllerRejected
from devstack.api.http import create_app
from devstack import update_control
from scripts import updater

ROOT = Path(__file__).resolve().parents[1]


class ManualUpdateHTTPTests(unittest.TestCase):
    def setUp(self):
        self.state = {"phase": "inactive", "project_id": None}
        def transport(op, **fields):
            if op == "status":
                return self.state.copy()
            raise ControllerRejected("ProtocolError")
        self.browser = create_app({
            "TESTING": True, "SECRET_KEY": "test",
            "SESSION_COOKIE_SECURE": False,
        }, transport=transport).test_client()
        self.patches = [
            patch("devstack.api.http.installed_version", return_value="v0.5.2"),
            patch("devstack.api.http.latest_version", return_value="v0.5.3"),
            patch("devstack.api.http.update_state", return_value={
                "phase": "idle", "unit_state": "inactive",
                "last_result": "success", "last_exit_code": "0",
            }),
            patch("devstack.api.http.request_update", return_value="Update requested"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.browser.get("/")
        with self.browser.session_transaction() as session:
            self.csrf = session["_csrf"]

    def test_release_status_and_mobile_update_controls(self):
        page = self.browser.get("/administration/updates")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"devstack-csrf", page.data)
        self.assertIn(b"DevStack", page.data)
        info = self.browser.get("/api/v2/devstack/update-status?check=1")
        self.assertEqual(info.status_code, 200)
        self.assertTrue(info.get_json()["update_available"])

    def test_csrf_and_no_arbitrary_payload_or_command(self):
        path = "/api/v2/devstack/update-now"
        self.assertEqual(self.browser.post(path, json={}).status_code, 403)
        self.assertEqual(self.browser.post(
            path, json={"tag": "v9.9.9"}, headers={"X-CSRF-Token": self.csrf},
        ).status_code, 400)
        self.assertEqual(self.browser.post(
            path, json={}, headers={"X-CSRF-Token": self.csrf},
        ).status_code, 202)
        self.assertEqual(self.browser.post(
            "/devstack/update-now", data={"_csrf": self.csrf},
        ).status_code, 405)
        from devstack.api import http
        http.request_update.assert_called()

    def test_active_project_or_error_state_refuses_restart(self):
        from devstack.api import http
        for phase, identifier in [("active", "any-id"), ("error", None),
                                  ("deactivating", "any-id")]:
            self.state.update(phase=phase, project_id=identifier)
            with self.subTest(phase=phase):
                response = self.browser.post(
                    "/api/v2/devstack/update-now", json={},
                    headers={"X-CSRF-Token": self.csrf},
                )
                self.assertEqual(response.status_code, 409)
        http.request_update.assert_not_called()

    def test_no_update_when_latest_is_not_newer(self):
        from devstack.api import http
        http.latest_version.return_value = "v0.5.2"
        response = self.browser.post(
            "/api/v2/devstack/update-now", json={},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 409)
        http.request_update.assert_not_called()

    def test_release_api_does_not_require_controller_for_readonly_status(self):
        self.state.update(phase="error", project_id="invalid")
        response = self.browser.get("/api/v2/devstack/update-status")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["latest"])


class MarkerSecurityTests(unittest.TestCase):
    def test_private_marker_and_duplicate_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            folder.chmod(0o700)
            with patch.dict(os.environ, {
                "DEVSTACK_UPDATE_REQUEST_DIR": str(folder),
            }), patch("devstack.update_control.update_state",
                      return_value={"phase": "idle"}):
                self.assertIn("requested", update_control.request_update().lower())
                marker = folder / "trigger"
                self.assertTrue(marker.is_file())
                self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
                self.assertIn("already queued", update_control.request_update())
            marker.unlink()
            folder.chmod(0o755)
            with patch.dict(os.environ, {
                "DEVSTACK_UPDATE_REQUEST_DIR": str(folder),
            }), self.assertRaises(update_control.UpdateControlError):
                update_control.request_update()

    def test_queued_or_running_update_does_not_create_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            folder.chmod(0o700)
            for phase in ("queued", "updating"):
                with self.subTest(phase=phase), patch.dict(os.environ, {
                    "DEVSTACK_UPDATE_REQUEST_DIR": str(folder),
                }), patch("devstack.update_control.update_state",
                          return_value={"phase": phase}):
                    self.assertIn("already", update_control.request_update())
                    self.assertFalse((folder / "trigger").exists())

    def test_installer_wires_systemd_path_watcher_not_privileged_web(self):
        install = (ROOT / "scripts/install.sh").read_text()
        web = (ROOT / "deploy/devstack-web-v2.service").read_text()
        path = (ROOT / "deploy/devstack-update-request.path").read_text()
        service = (ROOT / "deploy/devstack-update-request.service").read_text()
        self.assertIn("devstack-update-request.path", install)
        self.assertIn("systemctl enable --now devstack-update-request.path", install)
        self.assertIn("RuntimeDirectory=devstack-update-request", web)
        self.assertIn("RuntimeDirectoryMode=0700", web)
        self.assertNotIn("SupplementaryGroups=docker", web)
        self.assertIn("PathExists=/run/devstack-update-request/trigger", path)
        self.assertIn("ExecStart=/usr/bin/systemctl --no-block start devstack-update.service", service)
        self.assertIn("ExecStartPre=/usr/bin/rm -f /run/devstack-update-request/trigger", service)
        self.assertNotIn("@DEVSTACK_", path + service)
        subprocess.run(["bash", "-n", str(ROOT / "scripts/install.sh")],
                       check=True)


class TrustedUpdaterTests(unittest.TestCase):
    def test_existing_release_cannot_restart_active_or_unhealthy_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            old = root / "releases" / "v0.5.2"
            old.mkdir(parents=True)
            updater.point_at(root / "current", old)
            with patch("scripts.updater.healthy", return_value=False), patch(
                "scripts.updater.prepare_release"
            ) as prepared, self.assertRaisesRegex(RuntimeError, "Deactivate all projects"):
                updater.activate("v0.5.3", "rafaelpivato/devstack",
                                 root, Path(directory) / "state")
            self.assertEqual(updater.current_release(root / "current"), old)
            prepared.assert_not_called()

    def test_release_updater_enforces_tag_and_minimum_series(self):
        with self.assertRaises(ValueError):
            updater.checked_tag("main")
        with self.assertRaises(ValueError):
            updater.checked_tag("v0.5.2;sudo")
        with self.assertRaises(ValueError):
            updater.activate("v0.5.1", "rafaelpivato/devstack",
                             Path("/tmp/unused"), Path("/tmp/unused"))


if __name__ == "__main__":
    unittest.main()
