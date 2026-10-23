"""v0.6 browser bundle: release packaging and canonical Flask root."""
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

    def test_root_is_canonical_and_bundled(self):
        manifest = json.loads((STATIC / "manifest.json").read_text())
        entry = manifest["src/main.tsx"]
        self.assertTrue((STATIC / entry["file"]).is_file())
        self.assertGreater((STATIC / entry["file"]).stat().st_size, 100)
        css = entry.get("css") or [value["file"] for value in manifest.values()
                                   if value["file"].endswith(".css")]
        self.assertTrue(css)
        for asset in css:
            self.assertTrue((STATIC / asset).is_file())
        response = self.client.get("")
        self.assertEqual(response.status_code, 200)
        self.assertIn(entry["file"].encode(), response.data)
        for asset in css:
            self.assertIn(asset.encode(), response.data)
        self.assertIn(b'devstack-csrf', response.data)
        self.assertEqual(self.operations, [])  # page never activates/reads runtime
        self.assertEqual(self.client.get("/static/v06/" + entry["file"]).status_code, 200)
        # Client-side history paths are server-renderable after refresh and
        # never invoke a controller operation or mutate the global runtime.
        uid = "11111111-1111-4111-8111-111111111111"
        repo = "22222222-2222-4222-8222-222222222222"
        for route in ("/", "/projects",
                      f"/projects/{uid}",
                      f"/projects/{uid}/services",
                      f"/projects/{uid}/repositories/{repo}",
                      f"/projects/{uid}/repositories/add",
                      f"/projects/{uid}/repositories/bulk",
                      f"/projects/{uid}/repositories/branch",
                      f"/projects/{uid}/repositories/source",
                      f"/projects/{uid}/repositories/adds/{repo}",
                      f"/projects/{uid}/repositories/updates/{repo}"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)
        for route in ("/other", "/projects/not-an-id",
                      f"/projects/{uid}/terminal",
                      f"/projects/{uid}/services/anything",
                      f"/projects/{uid}/repositories/add/anything",
                      f"/projects/{uid}/repositories/adds/bad",
                      f"/projects/{uid}/repositories/updates/{repo}/logs",
                      "/unexpected/api/v2/runtime"):
            with self.subTest(invalid=route):
                self.assertEqual(self.client.get(route).status_code, 404)
        self.assertEqual(self.operations, [])

    def test_stable_api_and_csrf_guards_unchanged(self):
        # Only the existing JSON runtime GET calls the existing IPC operation.
        data = self.client.get("/api/v2/runtime").get_json()
        self.assertEqual(data["phase"], "inactive")
        self.assertEqual(self.operations, [("status", {})])
        self.assertEqual(self.client.post("/api/v2/runtime/deactivate").status_code, 403)
        self.assertEqual(self.operations, [("status", {})])


if __name__ == "__main__":
    unittest.main()
