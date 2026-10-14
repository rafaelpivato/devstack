import unittest
from unittest.mock import patch

from devstack.web import create_app
from devstack.update_control import UpdateControlError


class WebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()

    def test_secure_cookie_configuration(self):
        secure = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": True})
        self.assertTrue(secure.config["SESSION_COOKIE_SECURE"])

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

class SetupRoutesTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    @patch("devstack.web.public_key", return_value="ssh-ed25519 AAAATEST demo")
    def test_public_only_ssh_page(self, key):
        response = self.client.get("/settings/github")
        self.assertIn(b"ssh-ed25519 AAAATEST demo", response.data)
        self.assertNotIn(b"PRIVATE KEY", response.data)

    @patch("devstack.web.generate_key")
    def test_generate_key_requires_csrf(self, generate):
        response = self.client.post("/settings/github/generate")
        self.assertEqual(response.status_code, 403)
        generate.assert_not_called()
        response = self.client.post(
            "/settings/github/generate", data={"_csrf": self.csrf}
        )
        self.assertEqual(response.status_code, 302)
        generate.assert_called_once_with()

    @patch("devstack.web.create_project")
    def test_create_project(self, create):
        response = self.client.post("/projects/new",
                                    data={"_csrf": self.csrf, "name": "demo"})
        self.assertEqual(response.status_code, 302)
        create.assert_called_once_with("demo")

    @patch("devstack.web.add_github_repository")
    def test_add_repo(self, add):
        response = self.client.post("/projects/demo/repos/new", data={
            "_csrf": self.csrf, "url": "git@github.com:org/api.git",
            "name": "api", "branch": "develop",
        })
        self.assertEqual(response.status_code, 302)
        add.assert_called_once_with(
            "demo", "git@github.com:org/api.git",
            repo_name="api", branch="develop"
        )


class ManualUpdaterWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    @patch("devstack.web.request_update")
    def test_updater_csrf_enforced(self, update):
        response = self.client.post("/api/devstack/update-now")
        self.assertEqual(response.status_code, 403)
        update.assert_not_called()

    @patch("devstack.web.request_update", return_value="Update requested")
    def test_updater_returns_accepted(self, update):
        response = self.client.post(
            "/api/devstack/update-now", data={"_csrf": self.csrf}
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["message"], "Update requested")
        update.assert_called_once_with()

    @patch("devstack.web.request_update",
           side_effect=UpdateControlError("Run the one-time installer refresh"))
    def test_updater_error_is_visible(self, update):
        response = self.client.post(
            "/api/devstack/update-now", data={"_csrf": self.csrf}
        )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["ok"])

    @patch("devstack.web.check_request_directory")
    @patch("devstack.web.latest_version", return_value="v0.1.4")
    @patch("devstack.web.installed_version", return_value="v0.1.3")
    @patch("devstack.web.update_state", return_value={"phase": "idle"})
    def test_status(self, state, installed, latest, trigger):
        response = self.client.get("/api/devstack/update-status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["manual_available"])
        self.assertEqual(response.json["latest"], "v0.1.4")
