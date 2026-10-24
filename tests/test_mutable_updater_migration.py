"""Test one-time v0.6.5 host migration: refuse untrusted paths; atomic backup."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.promote_mutable_release_updater import (
    IDENTITY, promote, replace_owned_updater,
)


class PromoteUpdaterTests(unittest.TestCase):
    def root_stat(self, path, *args, **kwargs):
        result = self._original_stat(path, *args, **kwargs)
        values = list(result)
        values[4] = 0  # Simulate root ownership for a disposable fixture.
        return os.stat_result(values)

    def setUp(self):
        self._original_stat = Path.stat

    def test_unrecognized_old_updater_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "updater.py"
            destination.write_text("different vendor\n")
            with patch.object(Path, "stat", autospec=True, side_effect=self.root_stat):
                with self.assertRaisesRegex(RuntimeError, "Unrecognized"):
                    replace_owned_updater(destination, IDENTITY + b"\n")
            self.assertEqual(destination.read_text(), "different vendor\n")
            self.assertFalse((destination.with_name("updater.py.pre-mutable-backup")).exists())

    def test_invalid_python_does_not_replace_original(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "updater.py"
            original = b'"""Independent, conservative release updater"""\n'
            destination.write_bytes(original)
            payload = original + b"!syntaxerror"
            with patch.object(Path, "stat", autospec=True, side_effect=self.root_stat):
                with self.assertRaises(SyntaxError):
                    replace_owned_updater(destination, payload)
            self.assertEqual(destination.read_bytes(), original)
            self.assertFalse(destination.with_name("updater.py.pre-mutable-backup").exists())

    def test_valid_verified_payload_is_swapped_with_preserved_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "updater.py"
            previous = b'"""Independent, conservative release updater"""\nMARKER = 1\n'
            current = b'"""Independent, conservative release updater"""\nMARKER = 2\n'
            destination.write_bytes(previous)
            with patch.object(Path, "stat", autospec=True, side_effect=self.root_stat):
                replace_owned_updater(destination, current)
                self.assertEqual(destination.read_bytes(), current)
                backup = destination.with_name("updater.py.pre-mutable-backup")
                self.assertEqual(backup.read_bytes(), previous)
                replace_owned_updater(destination, current)
                self.assertEqual(backup.read_bytes(), previous)

    def test_root_and_published_tag_required(self):
        with patch("scripts.promote_mutable_release_updater.os.geteuid", return_value=1000):
            with self.assertRaisesRegex(RuntimeError, "root"):
                promote()

    def test_migration_is_not_a_runtime_update(self):
        source = (Path(__file__).resolve().parents[1] /
                  "scripts/promote_mutable_release_updater.py").read_text()
        self.assertIn('TAG = "v0.6.6"', source)
        self.assertIn('"v0.6.5"', source)
        self.assertIn("verified_wheel", source)
        self.assertIn("replace_owned_updater", source)
        self.assertNotIn("systemctl", source)


if __name__ == "__main__":
    unittest.main()
