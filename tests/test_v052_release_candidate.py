"""v0.5.2 candidate metadata and automatic publishing safety gate."""
from pathlib import Path
import re
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class V052ReleaseCandidateTests(unittest.TestCase):
    def test_version_matches_release_notes(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        version = metadata["project"]["version"]
        self.assertRegex(version, r"^[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertGreaterEqual(tuple(map(int,version.split("."))),(0,5,2))
        release = ROOT / "docs" / "releases" / f"v{version}.md"
        self.assertTrue(release.is_file())
        self.assertIn(f"v{version}", release.read_text())
        # Historical v0.5.2 cutover must stay documented after bumping.
        original = ROOT / "docs" / "releases" / "v0.5.2.md"
        self.assertIn("Clean Ubuntu installation required", original.read_text())

    def test_publication_is_only_from_main_after_docker_systemd_and_unit(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        self.assertRegex(workflow, r"(?m)^\s+branches:\s*\n\s+- main")
        self.assertIn("needs: [candidate, frontend-verification, docker-verification]", workflow)
        self.assertIn("uses: ./.github/workflows/docker-smoke.yml", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("gh release create", workflow)

    def test_no_legacy_migration_and_deferred_host_gate_is_explicit(self):
        installer = (ROOT / "scripts" / "install.sh").read_text()
        checklist = (ROOT / "docs" / "v0.5.2-release-checklist.md").read_text()
        self.assertIn('if numbers < (0, 5, 2):', installer)
        self.assertIn("An existing DevStack installation/state was detected", installer)
        for phrase in ("External acceptance", "second device",
                       "Crash/reboot", "Do not merge", "deferred to v0.5.3"):
            self.assertIn(phrase, checklist)


if __name__ == "__main__":
    unittest.main()
