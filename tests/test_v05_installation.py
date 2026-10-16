"""Fresh v0.5 installation and no-silent-upgrade safety contracts."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import os
import subprocess
import tempfile
import unittest

from devstack.compat_guard import IncompatibleInstallation, check_installation
from scripts import updater


ROOT = Path(__file__).resolve().parents[1]


class CleanInstallGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / "home"
        (self.home / ".config" / "devstack").mkdir(parents=True)
        self.config = self.home / ".config" / "devstack" / "workspace.toml"

    def test_new_empty_host_accepts_unprovisioned_workspace(self):
        check_installation(self.home, self.config)
        self.config.write_text("")
        check_installation(self.home, self.config)

    def test_new_single_project_is_valid(self):
        self.config.write_text('[project]\nname = "demo"\n')
        check_installation(self.home, self.config)

    def test_preexisting_v04_file_is_rejected_even_when_empty(self):
        (self.home / ".config" / "devstack" / "projects.toml").touch()
        with self.assertRaisesRegex(IncompatibleInstallation, "v0.4"):
            check_installation(self.home, self.config)

    def test_symlinked_old_config_is_not_followed(self):
        (self.home / ".config" / "devstack" / "projects.toml").symlink_to(
            self.config
        )
        with self.assertRaises(IncompatibleInstallation):
            check_installation(self.home, self.config)

    def test_legacy_environment_config_path_is_rejected(self):
        with self.assertRaisesRegex(IncompatibleInstallation, "projects.toml"):
            check_installation(self.home, self.home / "config" / "projects.toml")

    def test_multi_project_schema_never_accepted(self):
        self.config.write_text('[[projects]]\nname="demo"\n')
        with self.assertRaisesRegex(IncompatibleInstallation, "Invalid v0.5"):
            check_installation(self.home, self.config)

    def test_candidate_wsgi_refuses_old_updater_host_before_binding_http(self):
        # The *old* updater uses v0.4 systemd units without ExecStartPre.
        # The v0.5 WSGI app must refuse the candidate itself: health fails,
        # allowing the independent v0.4 updater to restore its previous link.
        from devstack.web import create_app
        from devstack.webhooks import create_webhook_app
        legacy = self.home / ".config" / "devstack" / "projects.toml"
        legacy.write_text('[[projects]]\nname = "demo"\n')
        with patch("devstack.compat_guard.Path.home", return_value=self.home):
            with patch.dict(os.environ, {"DEVSTACK_CONFIG": str(legacy)}):
                with self.assertRaises(IncompatibleInstallation):
                    create_app({"TESTING": True})
                with self.assertRaises(IncompatibleInstallation):
                    create_webhook_app({"TESTING": True})


class ReleaseSafetyTests(unittest.TestCase):
    def test_updater_series_and_tags(self):
        self.assertEqual(updater._series("v0.5.7"), (0, 5))
        self.assertEqual(updater._series("v0.6.0"), (0, 6))

    def test_new_updater_refuses_v04_to_v05_in_place(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "app"
            old = root / "releases" / "v0.4.0"
            old.mkdir(parents=True)
            updater.point_at(root / "current", old)
            with patch("scripts.updater.prepare_release") as prepare:
                with self.assertRaisesRegex(RuntimeError, "Refusing v0.4"):
                    updater.activate("v0.5.0", "rafaelpivato/devstack",
                                     root, Path(folder) / "state")
                prepare.assert_not_called()

    def test_later_minor_release_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "app"
            state = Path(folder) / "state"
            with (
                patch.dict(os.environ, {"DEVSTACK_ROOT": str(root),
                                        "DEVSTACK_STATE": str(state)}),
                patch("scripts.updater.latest_tag", return_value="v0.6.0"),
                patch("scripts.updater._current_tag", return_value="v0.5.7"),
                patch("scripts.updater.activate") as activate,
                patch("sys.argv", ["updater.py"]),
            ):
                self.assertEqual(updater.main(), 0)
                activate.assert_not_called()

    def test_install_script_pins_single_release_not_main(self):
        content = (ROOT / "scripts" / "install.sh").read_text()
        self.assertIn('DEVSTACK_RELEASE_TAG', content)
        self.assertIn('raw.githubusercontent.com/$repo/$release_tag', content)
        self.assertIn('updater.py --tag "$release_tag"', content)
        self.assertNotIn('raw.githubusercontent.com/$repo/main', content)
        self.assertIn('Existing v0.4', content)
        self.assertIn('workspace.toml', content)
        subprocess.run(["bash", "-n", str(ROOT / "scripts/install.sh")],
                       check=True)

    def test_both_wsgi_services_have_early_compatibility_guards(self):
        for unit in ("devstack.service", "devstack-webhooks.service"):
            with self.subTest(unit=unit):
                content = (ROOT / "deploy" / unit).read_text()
                self.assertIn("ExecStartPre=/opt/devstack/current/venv/bin/python -m devstack.compat_guard", content)

    def test_package_version_remains_on_planned_05_release_line(self):
        import tomllib
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertIn(data["project"]["version"], ("0.4.0", "0.5.0"))


if __name__ == "__main__":
    unittest.main()
