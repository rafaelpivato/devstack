import importlib.metadata
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from devstack import update_control as updates


class ManualUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "request"
        self.folder.mkdir(mode=0o700)
        self.path_patcher = patch("devstack.update_control.request_directory",
                                  return_value=self.folder)
        self.path_patcher.start()
        self.addCleanup(self.path_patcher.stop)
        updates._cache_value = None

    def test_update_request_is_marker_only_and_idempotent(self):
        self.assertIn("requested", updates.request_update().lower())
        self.assertEqual((self.folder / "trigger").stat().st_mode & 0o777, 0o600)
        self.assertIn("already queued", updates.request_update())
        self.assertEqual((self.folder / "trigger").read_bytes(), b"")

    def test_missing_watcher_requires_installer_refresh(self):
        self.folder.rmdir()
        with self.assertRaisesRegex(updates.UpdateControlError, "installer refresh"):
            updates.request_update()

    def test_reject_world_readable_dir(self):
        self.folder.chmod(0o755)
        with self.assertRaisesRegex(updates.UpdateControlError, "permissions"):
            updates.request_update()

    def test_reject_symlink_dir(self):
        self.folder.rmdir()
        self.folder.symlink_to(self.temp.name, target_is_directory=True)
        with self.assertRaisesRegex(updates.UpdateControlError, "permissions"):
            updates.request_update()

    @patch("devstack.update_control.subprocess.run")
    def test_service_state_running(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="ActiveState=activating\nResult=success\nExecMainStatus=0\n"
        )
        self.assertEqual(updates.update_state()["phase"], "updating")
        run.assert_called_once()
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("devstack.update_control.subprocess.run")
    def test_service_state_failed(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="ActiveState=failed\nResult=exit-code\nExecMainStatus=1\n"
        )
        self.assertEqual(updates.update_state()["phase"], "failed")

    @patch("devstack.update_control.urllib.request.urlopen")
    def test_latest_release_is_validated_and_cached(self, urlopen):
        class Context:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return json.dumps({"tag_name": "v1.2.3"}).encode()
        urlopen.return_value = Context()
        with patch.dict(os.environ, {"DEVSTACK_RELEASE_REPO": "example/devstack"}):
            self.assertEqual(updates.latest_version(), "v1.2.3")
            self.assertEqual(updates.latest_version(), "v1.2.3")
            self.assertEqual(urlopen.call_count, 1)

    @patch("devstack.update_control.urllib.request.urlopen")
    def test_unstable_release_is_rejected(self, urlopen):
        class Context:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"tag_name":"v1.2.3-rc1"}'
        urlopen.return_value = Context()
        with self.assertRaisesRegex(updates.UpdateControlError, "stable"):
            updates.latest_version()

    def test_version_reflects_installed_package(self):
        with patch.object(importlib.metadata, "version", return_value="0.1.4"):
            self.assertEqual(updates.installed_version(), "v0.1.4")
