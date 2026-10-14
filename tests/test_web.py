import unittest
from unittest.mock import patch

from devstack.web import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()

    def test_health(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")

    @patch("devstack.web.load_projects", return_value={})
    def test_empty_dashboard(self, _):
        response = self.client.get("/")
        self.assertIn(b"No projects configured", response.data)

    def test_post_without_csrf_is_rejected(self):
        response = self.client.post("/projects/anything/start")
        self.assertEqual(response.status_code, 403)

    @patch("devstack.web.load_projects", return_value={})
    def test_unknown_project_404_with_token(self, _):
        self.client.get("/")
        with self.client.session_transaction() as session:
            token = session["_csrf"]
        response = self.client.post("/projects/anything/start", data={"_csrf": token})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
