"""The release gate must validate CI-built source-only distribution wheels."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.verify_release_wheel import validate

ROOT = Path(__file__).resolve().parents[1]


class WheelPipelineContractTests(unittest.TestCase):
    def test_source_only_requires_ci_generated_assets(self):
        ignored = (ROOT / ".gitignore").read_text()
        self.assertIn("/src/devstack/static/v06/", ignored)
        frontend = (ROOT / ".github/workflows/frontend.yml").read_text()
        self.assertNotIn("Verify committed build is reproducible", frontend)
        self.assertIn("npm run build", frontend)
        self.assertIn("verify_release_wheel.py", frontend)
        guard = (ROOT / "setup.py").read_text()
        self.assertIn("Missing built React assets", guard)
        self.assertIn("setup()", guard)

    def test_no_wheel_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "Exactly one"):
                validate(Path(folder), ROOT)

    def test_release_workflow_requires_wheel_before_publishing(self):
        release = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("actions/download-artifact@v4", release)
        self.assertIn("SHA256SUMS", release)
        self.assertIn("actions/attest@v4", release)
        self.assertIn("--draft", release)
        self.assertIn("--draft=false", release)


if __name__ == "__main__":
    unittest.main()
