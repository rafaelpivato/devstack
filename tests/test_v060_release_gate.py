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
        self.assertEqual(version,"0.6.4")
        frontend=json.loads((ROOT/"frontend/package.json").read_text())
        self.assertEqual(frontend["version"],version)
        self.assertTrue((ROOT/"docs/releases"/f"v{version}.md").is_file())
        self.assertIn("legacy", (ROOT/"docs/releases"/f"v{version}.md").read_text().lower())
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
    def test_root_canonical_and_preview_bookmarks_redirect(self):
        calls=[]
        def transport(operation,**kw):
            calls.append(operation)
            raise AssertionError("GET must not contact controller")
        app=create_app({"TESTING":True,"SECRET_KEY":"test","SESSION_COOKIE_SECURE":False},
            transport=transport)
        with app.test_client() as client:
            home=client.get("/")
            self.assertEqual(home.status_code,200)
            self.assertIn(b'devstack-csrf',home.data)
            self.assertEqual(client.get("/projects/create").status_code,200)
            self.assertEqual(client.get("/projects/import").status_code,200)
            self.assertEqual(client.get("/__v06").status_code,308)
            self.assertEqual(client.get("/unknown").status_code,404)
            self.assertEqual(client.get("/api/v2/not-a-route").status_code,404)
            self.assertEqual(calls, [])

if __name__=="__main__":unittest.main()
