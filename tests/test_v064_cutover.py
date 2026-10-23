"""Root cutover must remain safe, bounded, and independent from controller availability."""
import unittest
from pathlib import Path
from uuid import uuid4
from devstack.api.http import create_app

ROOT = Path(__file__).resolve().parents[1]
UUID = str(uuid4())
CONTAINER = "a" * 64


class RootCutoverTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def transport(op, **kw):
            self.calls.append((op, kw))
            if op == "status":
                return {"phase": "inactive", "project_id": None}
            raise AssertionError("Unexpected controller operation: " + op)
        self.client = create_app({
            "TESTING": True, "SECRET_KEY": "testing",
            "SESSION_COOKIE_SECURE": False,
        }, transport=transport).test_client()

    def test_root_and_deep_links_are_read_only(self):
        paths = [
            "/", "/projects", "/projects/create", "/projects/import",
            "/projects/" + UUID, "/projects/" + UUID + "/services",
            "/projects/" + UUID + "/services/" + CONTAINER,
            "/projects/" + UUID + "/repositories/branch",
            "/projects/" + UUID + "/jobs/run/adhoc",
            "/administration", "/administration/diagnostics",
            "/runtime/operations", "/runtime/deactivate", "/imports/" + UUID,
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"devstack-csrf", response.data)
        self.assertEqual(self.calls, [])

    def test_unknown_routes_and_api_namespaces_cannot_be_shadowed(self):
        for path in ("/project", "/api", "/api/v2/unknown",
                     "/projects/invalid", "/projects/" + UUID + "/shell",
                     "/runtime/operations/invalid", "/__v06/projects/invalid",
                     "/__v06/api/v2/runtime", "/devstack/updates"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.calls, [])

    def test_preview_bookmarks_canonicalize_but_do_not_activate(self):
        for old, new in (
            ("/__v06", "/"), ("/__v06/", "/"),
            ("/__v06/projects", "/projects"),
            ("/__v06/administration/diagnostics", "/administration/diagnostics"),
            ("/__v06/projects/" + UUID + "/services",
             "/projects/" + UUID + "/services"),
        ):
            with self.subTest(old=old):
                response = self.client.get(old, follow_redirects=False)
                self.assertEqual(response.status_code, 308)
                self.assertEqual(response.headers["Location"], new)
        self.assertEqual(self.calls, [])

    def test_ui_only_files_gone_but_react_bundle_kept(self):
        templates = ROOT / "src/devstack/templates"
        self.assertTrue((templates / "app.html").is_file())
        self.assertFalse(list(templates.glob("v2_*.html")))
        self.assertFalse((templates / "v06_preview.html").exists())
        self.assertFalse((ROOT / "src/devstack/static/css/v2.css").exists())
        self.assertFalse((ROOT / "src/devstack/static/js/v2_dashboard.js").exists())
        self.assertTrue((ROOT / "src/devstack/static/v06/manifest.json").is_file())

    def test_csrf_and_updater_not_replaced_by_spa(self):
        self.assertEqual(self.client.post("/api/v2/runtime/deactivate").status_code, 403)
        self.assertEqual(self.client.post("/api/v2/devstack/update-now",
                                          json={}).status_code, 403)
        self.assertEqual(self.client.get("/api/v2/runtime").status_code, 200)
        self.assertEqual(self.calls, [("status", {})])


if __name__ == "__main__":
    unittest.main()
