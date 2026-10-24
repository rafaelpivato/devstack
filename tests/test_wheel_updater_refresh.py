"""Wheel-only releases must retain the root-only independent updater refresh path."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack.cli import install
from scripts import updater


class VersionedUpdaterRefreshTests(unittest.TestCase):
    def test_refresh_from_wheel_resource_and_reject_wrong_python(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "root"
            release = root / "releases/v0.6.6"
            venv = release / "venv"
            venv.mkdir(parents=True)
            updater.point_at(root / "current", release)

            # Model the selected wheel's installed devstack package layout.
            package = base / "site/devstack"
            module = package / "cli/install.py"
            module.parent.mkdir(parents=True)
            module.write_text("# test only")
            bundled = package / "_standalone_updater.py"
            trusted = b'#!/usr/bin/env python3\n"""Independent, conservative release updater. wheel."""\n'
            bundled.write_bytes(trusted)
            destination = base / "updater.py"
            old = b'#!/usr/bin/env python3\n"""Independent, conservative release updater. old."""\n'
            destination.write_bytes(old)

            original_stat = Path.stat
            def root_owned(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if path == destination:
                    fields = list(result)
                    fields[4] = 0
                    return os.stat_result(fields)
                return result

            with patch.object(install, "__file__", str(module)), patch.object(
                Path, "stat", root_owned
            ):
                with patch.object(install.sys, "prefix", str(base / "untrusted")):
                    with self.assertRaisesRegex(install.CLIInstallError, "selected release"):
                        install.refresh_updater(root, destination)
                self.assertEqual(destination.read_bytes(), old)
                with patch.object(install.sys, "prefix", str(venv)):
                    self.assertTrue(install.refresh_updater(root, destination))
                    self.assertFalse(install.refresh_updater(root, destination))
            self.assertEqual(destination.read_bytes(), trusted)
            self.assertEqual((base / "updater.py.pre-cli-backup").read_bytes(), old)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
