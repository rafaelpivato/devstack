"""Release-candidate invariants without running GitHub Actions or installing OS services."""
import json
import re
import tomllib
import unittest
from pathlib import Path
from devstack.api.http import create_app

ROOT=Path(__file__).resolve().parents[1]

class ReleaseCandidateGateTests(unittest.TestCase):
    def test_published_tag_metadata_and_notes_are_coherent(self):
        version=tomllib.loads((ROOT/"pyproject.toml").read_text())["project"]["version"]
        self.assertEqual(version,"0.6.2")
        frontend=json.loads((ROOT/"frontend/package.json").read_text())
        self.assertEqual(frontend["version"],version)
        self.assertTrue((ROOT/"docs/releases"/f"v{version}.md").is_file())
        self.assertIn("not", (ROOT/"docs/releases"/f"v{version}.md").read_text().lower())
    def test_main_only_release_requires_frontend_before_tagging(self):
        release=(ROOT/".github/workflows/release.yml").read_text()
        frontend=(ROOT/".github/workflows/frontend.yml").read_text()
        self.assertIn("branches:\n      - main",release)
        self.assertIn("uses: ./.github/workflows/frontend.yml",release)
        self.assertIn("needs: [candidate, frontend-verification, docker-verification]",release)
        self.assertIn("run: node scripts/verified-audit.mjs",frontend)
        script=(ROOT/"frontend/scripts/verified-audit.mjs").read_text()
        self.assertIn('"--audit-level=low"',script)
        self.assertIn("Security gate FAILED CLOSED",script)
        self.assertIn('kind:"vulnerable"',script)
        self.assertIn("maxAttempts=3",script)
        self.assertNotIn("--omit=dev",frontend)
        self.assertIn("workflow_call:",frontend)
        self.assertIn('gh release create "$TAG"',release)
    def test_release_candidate_retains_opt_in_new_ui_and_legacy_root(self):
        app=create_app({"TESTING":True,"SECRET_KEY":"test","SESSION_COOKIE_SECURE":False},
            transport=lambda operation,**kw: {
                "projects":{"projects":[]},"status":{"phase":"inactive","project_id":None},
                "github_ssh_status":{"public_key":None},
            }[operation])
        with app.test_client() as client:
            old=client.get("/")
            self.assertEqual(old.status_code,200)
            self.assertNotIn(b'src/main.tsx',old.data)
            new=client.get("/__v06")
            self.assertEqual(new.status_code,200)
            self.assertIn(b'devstack-csrf',new.data)
            self.assertEqual(client.get("/__v06/projects/create").status_code,200)
            self.assertEqual(client.get("/__v06/projects/import").status_code,200)
            self.assertEqual(client.get("/__v06/unknown").status_code,404)

if __name__=="__main__":unittest.main()
