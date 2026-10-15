import unittest
from unittest.mock import patch

from devstack.command_runner import CommandError
from devstack.web import create_app
from devstack.core import Project
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
        self.assertIn(b"No project configured", response.data)

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    def test_post_without_csrf_is_rejected(self, _):
        response = self.client.post("/project/action/start")
        self.assertEqual(response.status_code, 403)

    @patch("devstack.web.load_projects", return_value={})
    def test_unknown_project_404_with_token(self, _):
        self.client.get("/")
        with self.client.session_transaction() as session:
            token = session["_csrf"]
        response = self.client.post("/project/action/start", data={"_csrf": token})
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
        response = self.client.post("/project/new",
                                    data={"_csrf": self.csrf, "name": "demo"})
        self.assertEqual(response.status_code, 302)
        create.assert_called_once_with("demo")

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.add_github_repository")
    def test_add_repo(self, add, _):
        response = self.client.post("/project/repos/new", data={
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
           return_value={"demo": Project("demo", (), ())})
    @patch("devstack.web.read_compose", return_value="services:\n  web: {}")
    @patch("devstack.web.docker_available", return_value=True)
    @patch("devstack.web.current_job", return_value=None)
    def test_editor_shows_yaml_without_repo_changes(self, job, ready, read, projects):
        result = self.client.get("/project/docker")
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"services:", result.data)
        self.assertIn(b"Validate and save", result.data)

    @patch("devstack.web.load_projects",
           return_value={"demo": Project("demo", (), ())})
    @patch("devstack.web.save_compose")
    def test_compose_save_requires_csrf(self, save, projects):
        self.assertEqual(
            self.client.post("/project/docker/save", data={"compose":"foo"}).status_code,
            403
        )
        save.assert_not_called()
        self.assertEqual(
            self.client.post("/project/docker/save",
                             data={"_csrf":self.csrf,"compose":"services: {}"}).status_code,
            302
        )
        save.assert_called_once_with("demo","services: {}")

    @patch("devstack.web.load_projects",
           return_value={"demo": Project("demo", (), ())})
    @patch("devstack.web.queue_action")
    def test_compose_action_allowed(self, run, projects):
        result=self.client.post("/project/docker/action/build",
                                data={"_csrf":self.csrf})
        self.assertEqual(result.status_code, 302)
        run.assert_called_once_with("demo","build")

    @patch("devstack.web.import_smoke", return_value="smoke")
    def test_smoke_import(self, smoke):
        self.assertEqual(
            self.client.post("/project/smoke/import", data={"_csrf":self.csrf}).status_code,
            302
        )
        smoke.assert_called_once_with()

    @patch("devstack.web.load_projects",return_value={})
    def test_unknown_project_stays_404(self, projects):
        self.assertEqual(self.client.get("/project/docker").status_code, 404)


class ApplicationWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
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
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.add_application")
    def test_create_requires_csrf_and_never_calls_compose(self, add, projects):
        self.assertEqual(
            self.client.post("/project/applications/new", data={
                "application_name": "Frontend", "service": "web", "port": "5173"
            }).status_code, 403
        )
        add.assert_not_called()
        self.assertEqual(
            self.client.post("/project/applications/new", data={
                "_csrf": self.csrf, "application_name": "Frontend",
                "service": "web", "port": "5173"
            }).status_code, 302
        )
        add.assert_called_once_with("demo", "Frontend", "web", "5173")

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.update_application")
    def test_edit_does_not_modify_containers(self, change, projects):
        result = self.client.post("/project/applications/API/edit", data={
            "_csrf": self.csrf, "application_name": "Backend",
            "service": "backend", "port": ""
        })
        self.assertEqual(result.status_code, 302)
        change.assert_called_once_with("demo", "API", "Backend", "backend", "")

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.remove_application")
    def test_remove_does_not_modify_containers(self, remove, projects):
        result = self.client.post("/project/applications/API/remove",
                                  data={"_csrf": self.csrf})
        self.assertEqual(result.status_code, 302)
        remove.assert_called_once_with("demo", "API")

    @patch("devstack.web.load_projects", return_value={})
    def test_unknown_project_not_created(self, projects):
        result = self.client.post("/project/applications/new",
                                  data={"_csrf": self.csrf, "application_name": "API",
                                        "service": "api", "port": ""})
        self.assertEqual(result.status_code, 404)


    @patch("devstack.web.load_projects", return_value={
        "smoke": Project("smoke", (), ())
    })
    @patch("devstack.web.sync_smoke_applications", return_value=1)
    def test_sync_smoke_apps_is_csrf_protected(self, sync, projects):
        response = self.client.post("/project/smoke/sync-applications")
        self.assertEqual(response.status_code, 403)
        sync.assert_not_called()
        response = self.client.post("/project/smoke/sync-applications",
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
        response = self.client.post("/project/import", data={"url": path})
        self.assertEqual(response.status_code, 403)
        import_project.assert_not_called()
        response = self.client.post("/project/import", data={
            "_csrf": self.csrf, "url": path, "name": "smoke-git",
        })
        self.assertEqual(response.status_code, 302)
        import_project.assert_called_once_with(path, "smoke-git")

    @patch("devstack.web.import_github_project")
    def test_dashboard_shows_import_url_form(self, import_project):
        response = self.client.get("/")
        self.assertIn(b"Add project from GitHub", response.data)
        self.assertIn(b"/project/import", response.data)
        import_project.assert_not_called()


class CommandRunnerWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.command_services", return_value=("web", "redis"))
    @patch("devstack.web.load_tasks", return_value=())
    @patch("devstack.web.recent_jobs", return_value=[])
    def test_command_page(self, jobs, tasks, names, projects):
        result = self.client.get("/project/commands")
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"Run command", result.data)
        self.assertIn(b"redis", result.data)
        self.assertIn(b"Saved tasks", result.data)

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.start_command", return_value="a" * 20)
    def test_run_requires_csrf(self, start, projects):
        url = "/project/commands/run"
        form = {"service": "web", "mode": "exec", "command": "python --version"}
        self.assertEqual(self.client.post(url, data=form).status_code, 403)
        start.assert_not_called()
        form["_csrf"] = self.csrf
        response = self.client.post(url, data=form)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/commands/" + ("a" * 20), response.location)
        start.assert_called_once_with("demo", "web", "exec", "python --version", image="", network="none")

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.save_task")
    @patch("devstack.web.start_task", return_value="b" * 20)
    def test_saved_task_save_does_not_run(self, start, save, projects):
        data = {"_csrf": self.csrf, "task": "tests", "service": "web",
                "mode": "run", "command": "python -m unittest"}
        self.assertEqual(
            self.client.post("/project/commands/tasks/save", data=data).status_code,
            302,
        )
        save.assert_called_once_with("demo", "tests", "web", "run", "python -m unittest", image="", network="none")
        start.assert_not_called()
        self.assertEqual(
            self.client.post("/project/commands/tasks/run",
                             data={"_csrf": self.csrf, "task": "tests"}).status_code, 302
        )
        start.assert_called_once_with("demo", "tests")

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.command_job_info", return_value={
        "id": "c" * 20, "state": "running", "service": "web", "mode": "exec",
        "argv": ["python", "--version"],
    })
    @patch("devstack.web.command_job_output", return_value="Python 3.12")
    @patch("devstack.web.cancel_command")
    def test_status_and_cancel(self, cancel, output, info, projects):
        url = "/api/v1/project/commands/" + ("c" * 20)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["output"], "Python 3.12")
        self.assertEqual(
            self.client.post("/project/commands/" + ("c" * 20) + "/cancel").status_code, 403
        )
        cancel.assert_not_called()
        self.assertEqual(
            self.client.post("/project/commands/" + ("c" * 20) + "/cancel",
                             data={"_csrf": self.csrf}).status_code, 302
        )
        cancel.assert_called_once_with("demo", "c" * 20)


class StandaloneImageWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.command_services", side_effect=CommandError("No Compose file"))
    @patch("devstack.web.load_tasks", return_value=())
    @patch("devstack.web.recent_jobs", return_value=[])
    def test_standalone_form_available_without_compose(self, history, tasks, services, projects):
        response = self.client.get("/project/commands")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Standalone Docker image", response.data)
        self.assertIn(b'name="target"', response.data)
        self.assertIn(b'name="network"', response.data)

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.start_command", return_value="a" * 20)
    def test_standalone_image_run_is_forced_to_run_mode(self, command, projects):
        values = {"target": "image", "image": "alpine:3.20", "network": "none",
                  "command": "echo hello"}
        self.assertEqual(self.client.post("/project/commands/run",
                                          data=values).status_code, 403)
        command.assert_not_called()
        values["_csrf"] = self.csrf
        result = self.client.post("/project/commands/run", data=values)
        self.assertEqual(result.status_code, 302)
        command.assert_called_once_with(
            "demo", "", "run", "echo hello", image="alpine:3.20", network="none"
        )

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.save_task")
    def test_image_task_save_forces_run_mode(self, save, projects):
        values = {"_csrf": self.csrf, "target": "image", "task": "db",
                  "image": "postgres:16", "network": "project",
                  "command": "pg_isready -h postgres"}
        response = self.client.post("/project/commands/tasks/save", data=values)
        self.assertEqual(response.status_code, 302)
        save.assert_called_once_with(
            "demo", "db", "", "run", "pg_isready -h postgres",
            image="postgres:16", network="project"
        )


class ProjectConfigurationWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    def project(self):
        from devstack.core import Project, Repo
        from pathlib import Path
        return Project("demo", (Repo("backend", Path("/workspace/demo/backend")),), ())

    @patch("devstack.web.load_projects")
    @patch("devstack.web.source_directory")
    @patch("devstack.web.sync_review")
    def test_review_is_non_mutating(self, review, source, projects):
        from pathlib import Path
        from devstack.project_sync import Change
        projects.return_value = {"demo": self.project()}
        source.return_value = Path("/workspace/demo/backend/.devstack")
        review.return_value = ([Change(
            "tasks.toml:lint", "Task", "lint", "add", "(not present)",
            '{"command":["python","-m","ruff"]}', False, True
        )], "a" * 64)
        result = self.client.get("/project/configuration")
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"Update source from Git", result.data)
        self.assertIn(b"tasks.toml:lint", result.data)
        self.assertIn(b'checked', result.data)
        review.assert_called_once_with("demo")

    @patch("devstack.web.load_projects")
    @patch("devstack.web.source_directory")
    @patch("devstack.web.repo_update", return_value="main")
    def test_update_source_only_touches_linked_repo(self, update, source, projects):
        from pathlib import Path
        projects.return_value = {"demo": self.project()}
        source.return_value = Path("/workspace/demo/backend/examples/smoke/.devstack")
        url = "/project/configuration/update-source"
        self.assertEqual(self.client.post(url).status_code, 403)
        update.assert_not_called()
        self.assertEqual(
            self.client.post(url, data={"_csrf": self.csrf}).status_code, 302
        )
        update.assert_called_once_with(self.project().repos[0])

    @patch("devstack.web.load_projects")
    @patch("devstack.web.sync_apply", return_value=2)
    def test_apply_only_explicitly_selected_changes_with_token(self, apply, projects):
        projects.return_value = {"demo": self.project()}
        url = "/project/configuration/apply"
        payload = {"fingerprint": "a" * 64, "change": [
            "tasks.toml:lint", "project.toml:API"
        ]}
        self.assertEqual(self.client.post(url, data=payload).status_code, 403)
        apply.assert_not_called()
        payload["_csrf"] = self.csrf
        self.assertEqual(self.client.post(url, data=payload).status_code, 302)
        apply.assert_called_once_with("demo",
            ["tasks.toml:lint", "project.toml:API"], "a" * 64)

    @patch("devstack.web.load_projects", return_value={})
    def test_missing_project_returns_not_found(self, projects):
        self.assertEqual(self.client.get("/project/configuration").status_code, 404)



class PublicWebhookAdminWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "unit-test"})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.load_webhooks", return_value=())
    @patch("devstack.web.load_applications", return_value=(
        __import__("devstack.applications", fromlist=["Application"]).Application(
            "API", "backend", 8000
        ),
    ))
    def test_route_management_page(self, apps, routes, projects):
        response = self.client.get("/project/webhooks")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Public webhooks", response.data)
        self.assertIn(b"API", response.data)
        self.assertIn(b"Adding a route does", response.data)

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.add_webhook")
    def test_write_requires_csrf(self, add, projects):
        url = "/project/webhooks/new"
        payload = {"webhook_name": "stripe", "application": "API",
                   "path": "/webhooks/stripe"}
        self.assertEqual(self.client.post(url, data=payload).status_code, 403)
        add.assert_not_called()
        payload["_csrf"] = self.csrf
        self.assertEqual(self.client.post(url, data=payload).status_code, 302)
        add.assert_called_once_with("demo", "stripe", "API", "/webhooks/stripe")

    @patch("devstack.web.load_projects", return_value={
        "demo": Project("demo", (), ())
    })
    @patch("devstack.web.remove_webhook")
    def test_remove_requires_csrf(self, remove, projects):
        url = "/project/webhooks/stripe/remove"
        self.assertEqual(self.client.post(url).status_code, 403)
        remove.assert_not_called()
        self.assertEqual(self.client.post(url, data={"_csrf": self.csrf}).status_code, 302)
        remove.assert_called_once_with("demo", "stripe")
