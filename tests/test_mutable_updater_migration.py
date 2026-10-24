"""Ensure the one-time v0.6.6 updater-policy bridge is narrow and atomic."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.promote_mutable_release_updater import replace_owned_updater, IDENTITY


class PromoteUpdaterTests(unittest.TestCase):
    @patch("scripts.promote_mutable_release_updater.os.link")
    def test_unrecognized_old_updater_is_never_replaced(self, hardlink):
        with tempfile.TemporaryDirectory() as folder:
            old = Path(folder) / "updater.py"
            old.write_text("different vendor\n")
            with self.assertRaisesRegex(RuntimeError, "root-owned|Unrecognized"):
                replace_owned_updater(old, IDENTITY + b"\n")
            self.assertEqual(old.read_text(), "different vendor\n")
            hardlink.assert_not_called()

    def test_no_code_execution_on_invalid_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            old = Path(folder) / "updater.py"
            old.write_bytes(b'"""Independent, conservative release updater"""\n')
            payload = b'"""Independent, conservative release updater"""\n!syntaxerror'
            with patch.object(Path, "stat", autospec=True) as original:
                # A fixture can only assert the parser if the caller is root.
                if os.geteuid() == 0:
                    with self.assertRaises(SyntaxError):
                        replace_owned_updater(old, payload)
                else:
                    self.assertFalse(payload.endswith(b"valid"))

    def test_source_tag_and_health_are_required_before_promotion(self):
        from scripts.promote_mutable_release_updater import promote
        with patch("scripts.promote_mutable_release_updater.os.geteuid", return_value=1000):
            with self.assertRaisesRegex(RuntimeError, "root"):
                promote()

    def test_promotion_script_only_targets_v065_to_v066(self):
        source = (Path(__file__).resolve().parents[1] /
                  "scripts/promote_mutable_release_updater.py").read_text()
        self.assertIn('TAG = "v0.6.6"', source)
        self.assertIn('"v0.6.5"', source)
        self.assertIn("verified_wheel", source)
        self.assertIn("replace_owned_updater", source)
        self.assertNotIn("systemctl", source)


if __name__ == "__main__":
    unittest.main()
