"""R1 immutable release-candidate metadata, safe publication, and CLI packaging."""
from pathlib import Path
import re
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class V053CandidateTests(unittest.TestCase):
    def test_single_staged_release_version_and_notes(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        version=metadata["project"]["version"]
        self.assertGreaterEqual(tuple(map(int,version.split("."))),(0,5,3))
        self.assertTrue((ROOT/"docs/releases"/f"v{version}.md").is_file())
        notes = (ROOT / "docs/releases/v0.5.3.md").read_text()
        self.assertIn("# DevStack v0.5.3", notes)
        self.assertIn("Release candidate", notes)
        self.assertIn("v0.5.2", notes)
        self.assertIn("v0.5.1", notes)
        self.assertIn("--refresh-updater", notes)
        self.assertIn("devstackipc", notes)
        self.assertIn("not been", notes)
        self.assertEqual(metadata["project"]["scripts"]["dev"],
                         "devstack.cli.main:main")

    def test_final_release_workflow_is_main_only_and_requires_gates(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertRegex(workflow, r"(?m)^\s+branches:\s*\n\s+- main\s*$")
        self.assertIn("needs: [candidate, frontend-verification, docker-verification]", workflow)
        self.assertIn("uses: ./.github/workflows/docker-smoke.yml", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn('gh release create "$TAG"', workflow)
        docker = (ROOT / ".github/workflows/docker-smoke.yml").read_text()
        self.assertIn("Verify v0.5.2 real Docker A-to-B-to-A switching", docker)
        self.assertIn("Fresh Ubuntu systemd and Unix-socket permissions", docker)

    def test_no_controller_db_migration_or_privileged_cli_dependencies(self):
        from devstack.storage.database import SCHEMA_VERSION
        self.assertEqual(SCHEMA_VERSION, 10)  # unchanged v0.5.2 registry schema
        cli = (ROOT / "src/devstack/cli/main.py").read_text()
        git = (ROOT / "src/devstack/cli/git_commands.py").read_text()
        for module in (cli, git):
            for forbidden in ("import docker", "import subprocess",
                              "import sqlite3", "from devstack.control"):
                self.assertNotIn(forbidden, module)
        installer = (ROOT / "src/devstack/cli/install.py").read_text()
        self.assertIn("refresh_updater", installer)
        self.assertIn("pre-cli-backup", installer)

    def test_historical_v052_cutover_is_preserved(self):
        legacy = (ROOT / "docs/releases/v0.5.2.md").read_text()
        self.assertIn("Clean Ubuntu installation required", legacy)
        installer = (ROOT / "scripts/install.sh").read_text()
        self.assertIn("An existing DevStack installation/state was detected",
                      installer)
        readme = (ROOT / "README.md").read_text()
        self.assertIn("v0.5.3", readme)
        self.assertIn("Latest published: v0.6.4", readme)
        self.assertIn("v0.6.4", readme)
        self.assertIn("docs/releases/v0.5.3.md", readme)


if __name__ == "__main__":
    unittest.main()
