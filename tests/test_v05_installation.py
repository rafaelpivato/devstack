"""Portable fresh install and uninterrupted automatic release following."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import updater

ROOT = Path(__file__).resolve().parents[1]


class AutoUpdateTests(unittest.TestCase):
    def test_same_version_is_not_reinstalled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            current = root / "releases" / "v0.5.7"
            current.mkdir(parents=True)
            (current / ".ready").write_text("v0.5.7\n")
            updater.point_at(root / "current", current)
            with (patch("scripts.updater.prepare_release") as prepare,
                  patch("scripts.updater.healthy", return_value=True)):
                message = updater.activate("v0.5.7", "rafaelpivato/devstack",
                                           root, Path(directory) / "state")
                self.assertIn("Already running", message)
                prepare.assert_not_called()

    def test_same_version_with_active_project_does_not_report_update_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            current = root / "releases" / "v0.5.7"
            current.mkdir(parents=True)
            (current / ".ready").write_text("v0.5.7\n")
            updater.point_at(root / "current", current)
            with (patch("scripts.updater.prepare_release") as prepare,
                  patch("scripts.updater.healthy", return_value=False) as health):
                self.assertIn("Already running", updater.activate(
                    "v0.5.7", "rafaelpivato/devstack",
                    root, Path(directory) / "state",
                ))
                prepare.assert_not_called()
                health.assert_not_called()

    def test_new_minor_release_is_automatically_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            old = root / "releases" / "v0.5.7"
            new = root / "releases" / "v0.6.0"
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            (new / ".ready").write_text("v0.6.0\n")
            updater.point_at(root / "current", old)
            with (patch("scripts.updater.execute"),
                  patch("scripts.updater.healthy", return_value=True)):
                message = updater.activate("v0.6.0", "rafaelpivato/devstack",
                                           root, Path(directory) / "state")
            self.assertEqual(updater.current_release(root / "current"), new)
            self.assertIn("Activated v0.6.0", message)

    def test_timer_follows_latest_tag_across_release_series(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            state = Path(directory) / "state"
            with (
                patch.dict(os.environ, {"DEVSTACK_ROOT": str(root),
                                        "DEVSTACK_STATE": str(state)}),
                patch("scripts.updater.latest_tag", return_value="v0.6.0") as latest,
                patch("scripts.updater.activate", return_value="Activated v0.6.0") as activate,
                patch("sys.argv", ["updater.py"]),
            ):
                self.assertEqual(updater.main(), 0)
            latest.assert_called_once_with("rafaelpivato/devstack")
            self.assertEqual(activate.call_args.args[:2],
                             ("v0.6.0", "rafaelpivato/devstack"))

    def test_failed_upgrade_still_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            previous = root / "releases" / "v0.5.7"
            target = root / "releases" / "v0.6.0"
            previous.mkdir(parents=True)
            target.mkdir(parents=True)
            (target / ".ready").write_text("v0.6.0\n")
            updater.point_at(root / "current", previous)
            with (
                patch("scripts.updater.execute"),
                patch("scripts.updater.healthy", side_effect=[True, False]),
                patch("scripts.updater.subprocess.run") as process,
            ):
                with self.assertRaisesRegex(RuntimeError, "restored previous"):
                    updater.activate("v0.6.0", "rafaelpivato/devstack",
                                     root, Path(directory) / "state")
            self.assertEqual(updater.current_release(root / "current"), previous)
            self.assertTrue((Path(directory) / "state" / "failed-v0.6.0").exists())
            process.assert_not_called()

    def test_installer_fetches_latest_once_but_not_a_permanent_pin(self):
        script = (ROOT / "scripts" / "install.sh").read_text()
        self.assertIn("/releases/latest", script)
        self.assertIn("raw.githubusercontent.com/$repo/$release_tag", script)
        self.assertIn('updater.py --tag "$release_tag"', script)
        self.assertIn("devstack-update.timer", script)
        self.assertNotIn("DEVSTACK_RELEASE_TAG", script)
        self.assertNotIn('raw.githubusercontent.com/$repo/main', script)
        self.assertNotIn("Existing v0.4", script)
        subprocess.run(["bash", "-n", str(ROOT / "scripts/install.sh")],
                       check=True)

    def test_no_upgrade_series_lock_or_legacy_boot_guard(self):
        script = (ROOT / "scripts" / "updater.py").read_text()
        self.assertNotIn("def _series(", script)
        self.assertNotIn("Skipping incompatible automatic release", script)
        self.assertFalse((ROOT / "src" / "devstack" / "compat_guard.py").exists())
        for unit in ("devstack-control.service", "devstack-web-v2.service"):
            self.assertNotIn("devstack.compat_guard",
                             (ROOT / "deploy" / unit).read_text())
        for module in ("api/http.py", "control/daemon.py"):
            self.assertNotIn("check_installation(",
                             (ROOT / "src" / "devstack" / module).read_text())

    def test_publication_waits_for_docker_and_unit_tests(self):
        release = (ROOT / ".github/workflows/release.yml").read_text()
        docker = (ROOT / ".github/workflows/docker-smoke.yml").read_text()
        self.assertIn("workflow_call:", docker)
        self.assertIn("uses: ./.github/workflows/docker-smoke.yml", release)
        self.assertIn("needs: [candidate, frontend-verification, docker-verification]", release)
        self.assertIn("python -m unittest discover -s tests -v", release)

    def test_current_patch_release_version(self):
        import tomllib
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        self.assertRegex(version, r"^[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertGreaterEqual(tuple(map(int, version.split("."))), (0, 5, 2))
        self.assertTrue((ROOT / "docs" / "releases" / f"v{version}.md").is_file())


if __name__ == "__main__":
    unittest.main()
