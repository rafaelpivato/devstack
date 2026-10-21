"""v0.6 browser bundle: release packaging and safe, opt-in Flask preview."""
import json
import unittest
from pathlib import Path

from devstack.api.http import create_app


STATIC = Path(__file__).resolve().parents[1] / "src/devstack/static/v06"


class FrontendFoundationTests(unittest.TestCase):
    def setUp(self):
        self.operations = []
        def transport(op, **fields):
            self.operations.append((op, fields))
            return {"phase": "inactive", "project_id": None}
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test",
                               "SESSION_COOKIE_SECURE": False}, transport=transport)
        self.client = self.app.test_client()

    def test_preview_is_opt_in_and_bundled(self):
        manifest = json.loads((STATIC / "manifest.json").read_text())
        entry = manifest["src/main.tsx"]
        self.assertTrue((STATIC / entry["file"]).is_file())
        self.assertGreater((STATIC / entry["file"]).stat().st_size, 100)
        self.assertTrue(entry.get("css"))
        for asset in entry["css"]:
            self.assertTrue((STATIC / asset).is_file())
        response = self.client.get("/__v06")
        self.assertEqual(response.status_code, 200)
        self.assertIn(entry["file"].encode(), response.data)
        for asset in entry["css"]:
            self.assertIn(asset.encode(), response.data)
        self.assertIn(b'devstack-csrf', response.data)
        self.assertEqual(self.operations, [])  # preview never activates/reads runtime
        self.assertEqual(self.client.get("/static/v06/" + entry["file"]).status_code, 200)
        self.assertEqual(self.client.get("/__v06/other").status_code, 404)

    def test_stable_api_and_csrf_guards_unchanged(self):
        # Only the existing JSON runtime GET calls the existing IPC operation.
        data = self.client.get("/api/v2/runtime").get_json()
        self.assertEqual(data["phase"], "inactive")
        self.assertEqual(self.operations, [("status", {})])
        self.assertEqual(self.client.post("/api/v2/runtime/deactivate").status_code, 403)
        self.assertEqual(self.operations, [("status", {})])


if __name__ == "__main__":
    unittest.main()
