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


class DockerWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    @patch("devstack.web.load_projects",
           return_value={"demo": __import__("devstack.core", fromlist=["Project"]).Project("demo", (), ())})
    @patch("devstack.web.read_compose", return_value="services:\n  web: {}")
    @patch("devstack.web.docker_available", return_value=True)
    @patch("devstack.web.current_job", return_value=None)
    def test_editor_shows_yaml_without_repo_changes(self, job, ready, read, projects):
        result = self.client.get("/projects/demo/docker")
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"services:", result.data)
        self.assertIn(b"Validate and save", result.data)

    @patch("devstack.web.load_projects",
           return_value={"demo": __import__("devstack.core", fromlist=["Project"]).Project("demo", (), ())})
    @patch("devstack.web.save_compose")
    def test_compose_save_requires_csrf(self, save, projects):
        self.assertEqual(
            self.client.post("/projects/demo/docker/save", data={"compose":"foo"}).status_code,
            403
        )
        save.assert_not_called()
        self.assertEqual(
            self.client.post("/projects/demo/docker/save",
                             data={"_csrf":self.csrf,"compose":"services: {}"}).status_code,
            302
        )
        save.assert_called_once_with("demo","services: {}")

    @patch("devstack.web.load_projects",
           return_value={"demo": __import__("devstack.core", fromlist=["Project"]).Project("demo", (), ())})
    @patch("devstack.web.queue_action")
    def test_compose_action_allowed(self, run, projects):
        result=self.client.post("/projects/demo/docker/action/build",
                                data={"_csrf":self.csrf})
        self.assertEqual(result.status_code, 302)
        run.assert_called_once_with("demo","build")

    @patch("devstack.web.import_smoke", return_value="smoke")
    def test_smoke_import(self, smoke):
        self.assertEqual(
            self.client.post("/projects/smoke/import", data={"_csrf":self.csrf}).status_code,
            302
        )
        smoke.assert_called_once_with()

    @patch("devstack.web.load_projects",return_value={})
    def test_unknown_project_stays_404(self, projects):
        self.assertEqual(self.client.get("/projects/nope/docker").status_code, 404)


class ApplicationWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    @patch("devstack.web.load_projects", return_value={
        "demo": __import__("devstack.core", fromlist=["Project"]).Project("demo", (), ())
    })
    @patch("devstack.web.load_applications", return_value=(
        __import__("devstack.applications", fromlist=["Application"]).Application(
            "API", "backend", 8000
        ),
    ))
    @patch("devstack.web.compose_states", return_value={"backend": "running"})
    def test_dashboard_shows_registered_app_not_infrastructure(self, states, apps, projects):
        result = self.client.get("/")
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"API", result.data)
        self.assertIn(b"backend", result.data)
        self.assertIn(b"running", result.data)
        self.assertIn(b"1 applications", result.data)
        self.assertNotIn(b"PostgreSQL", result.data)
        self.assertNotIn(b"No services configured", result.data)
        states.assert_called_once_with("demo")

    @patch("devstack.web.load_projects", return_value={
        "demo": __import__("devstack.core", fromlist=["Project"]).Project("demo", (), ())
    })
    @patch("devstack.web.add_application")
    def test_create_requires_csrf_and_never_calls_compose(self, add, projects):
        self.assertEqual(
            self.client.post("/projects/demo/applications/new", data={
                "application_name": "Frontend", "service": "web", "port": "5173"
            }).status_code, 403
        )
        add.assert_not_called()
        self.assertEqual(
            self.client.post("/projects/demo/applications/new", data={
                "_csrf": self.csrf, "application_name": "Frontend",
                "service": "web", "port": "5173"
            }).status_code, 302
        )
        add.assert_called_once_with("demo", "Frontend", "web", "5173")

    @patch("devstack.web.load_projects", return_value={
        "demo": __import__("devstack.core", fromlist=["Project"]).Project("demo", (), ())
    })
    @patch("devstack.web.update_application")
    def test_edit_does_not_modify_containers(self, change, projects):
        result = self.client.post("/projects/demo/applications/API/edit", data={
            "_csrf": self.csrf, "application_name": "Backend",
            "service": "backend", "port": ""
        })
        self.assertEqual(result.status_code, 302)
        change.assert_called_once_with("demo", "API", "Backend", "backend", "")

    @patch("devstack.web.load_projects", return_value={
        "demo": __import__("devstack.core", fromlist=["Project"]).Project("demo", (), ())
    })
    @patch("devstack.web.remove_application")
    def test_remove_does_not_modify_containers(self, remove, projects):
        result = self.client.post("/projects/demo/applications/API/remove",
                                  data={"_csrf": self.csrf})
        self.assertEqual(result.status_code, 302)
        remove.assert_called_once_with("demo", "API")

    @patch("devstack.web.load_projects", return_value={})
    def test_unknown_project_not_created(self, projects):
        result = self.client.post("/projects/missing/applications/new",
                                  data={"_csrf": self.csrf, "application_name": "API",
                                        "service": "api", "port": ""})
        self.assertEqual(result.status_code, 404)


    @patch("devstack.web.load_projects", return_value={
        "smoke": __import__("devstack.core", fromlist=["Project"]).Project("smoke", (), ())
    })
    @patch("devstack.web.sync_smoke_applications", return_value=1)
    def test_sync_smoke_apps_is_csrf_protected(self, sync, projects):
        response = self.client.post("/projects/smoke/sync-applications")
        self.assertEqual(response.status_code, 403)
        sync.assert_not_called()
        response = self.client.post("/projects/smoke/sync-applications",
                                    data={"_csrf": self.csrf})
        self.assertEqual(response.status_code, 302)
        sync.assert_called_once_with()


class GithubProjectImportWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    @patch("devstack.web.import_github_project", return_value=("smoke-git", "main"))
    def test_import_project_requires_csrf(self, import_project):
        path = "https://github.com/rafaelpivato/devstack/tree/main/examples/smoke"
        response = self.client.post("/projects/import", data={"url": path})
        self.assertEqual(response.status_code, 403)
        import_project.assert_not_called()
        response = self.client.post("/projects/import", data={
            "_csrf": self.csrf, "url": path, "name": "smoke-git",
        })
        self.assertEqual(response.status_code, 302)
        import_project.assert_called_once_with(path, "smoke-git")

    @patch("devstack.web.import_github_project")
    def test_dashboard_shows_import_url_form(self, import_project):
        response = self.client.get("/")
        self.assertIn(b"Import GitHub project", response.data)
        self.assertIn(b"/projects/import", response.data)
        import_project.assert_not_called()
