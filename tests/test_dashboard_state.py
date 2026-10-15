"""v0.5 dashboard JSON contract and framework-independent state assembly."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from devstack import dashboard_state as dashboard
from devstack.applications import Application, ApplicationError
from devstack.container_inventory import compose_containers
from devstack.dev_ports import PublishedPort
from devstack.workspace import Project, Repo
from devstack.webhooks import Webhook


class DashboardStateTests(unittest.TestCase):
    def test_empty_workspace_uses_stable_json_shape(self):
        with patch("devstack.dashboard_state.runtime.status",
                   return_value={"phase": "inactive", "project": None,
                                 "token": "never-leak", "boot_id": "private"}):
            data = dashboard.overview(None)
        self.assertEqual(data["schema_version"], 1)
        self.assertIsNone(data["project"])
        self.assertEqual(data["runtime"], {"phase": "inactive", "project": None})
        for section in ("repositories", "applications", "services", "containers",
                        "ports", "tasks", "recent_jobs", "webhooks"):
            self.assertEqual(data[section], [])
        self.assertEqual(data["errors"], {})

    def test_single_workspace_collects_compose_and_presentation_data(self):
        from devstack.command_runner import Task
        project = Project("demo", (Repo("api", Path("/tmp/api")),))
        with (
            patch("devstack.dashboard_state.runtime.status",
                  return_value={"phase": "active", "project": "demo",
                                "token": "secret", "boot_id": "secret"}),
            patch("devstack.dashboard_state.repo_branch", return_value="main"),
            patch("devstack.dashboard_state.repo_dirty", return_value=False),
            patch("devstack.dashboard_state.load_applications",
                  return_value=(Application("API", "backend", 8000),)),
            patch("devstack.dashboard_state.compose_states",
                  return_value={"backend": "running", "redis": "running"}),
            patch("devstack.dashboard_state.compose_containers", return_value=(
                {"name": "demo-api-1", "service": "backend", "state": "running",
                 "health": "healthy"},
            )),
            patch("devstack.dashboard_state.published_ports", return_value=(
                PublishedPort("backend", 8000, 34567, "http://100.80.1.2:34567/"),
            )),
            patch("devstack.dashboard_state.load_tasks", return_value=(
                Task("lint", "backend", "exec", ("python", "-m", "unittest")),
            )),
            patch("devstack.dashboard_state.recent_jobs",
                  return_value=[{"id": "a"*20, "state": "success",
                                 "service": "backend", "mode": "exec",
                                 "argv": ["sensitive", "arg"], "started": 4}]),
            patch("devstack.dashboard_state.current_job",
                  return_value={"id": "b"*16, "action": "build",
                                "state": "success", "log": "secret"}),
            patch("devstack.dashboard_state.load_webhooks",
                  return_value=(Webhook("stripe", "API", "/events"),)),
        ):
            data = dashboard.overview(project)
        self.assertEqual(data["project"], {"name": "demo"})
        self.assertEqual(data["repositories"][0]["branch"], "main")
        self.assertEqual(data["services"][0], {"name": "backend", "state": "running"})
        self.assertEqual(data["applications"][0]["state"], "running")
        self.assertEqual(data["containers"][0]["name"], "demo-api-1")
        self.assertEqual(data["ports"][0]["host_port"], 34567)
        self.assertEqual(data["tasks"][0]["name"], "lint")
        self.assertEqual(data["webhooks"][0]["enabled"], True)
        self.assertEqual(data["errors"], {})
        self.assertNotIn("argv", data["recent_jobs"][0])
        self.assertNotIn("log", data["compose_job"])
        self.assertNotIn("token", data["runtime"])
        self.assertNotIn("boot_id", data["runtime"])

    def test_inactive_project_never_enumerates_ports_or_containers(self):
        project = Project("demo", ())
        with (
            patch("devstack.dashboard_state.runtime.status",
                  return_value={"phase": "inactive", "project": "demo"}),
            patch("devstack.dashboard_state.load_applications",
                  return_value=(Application("API", "backend", None),)),
            patch("devstack.dashboard_state.compose_states") as services,
            patch("devstack.dashboard_state.compose_containers") as containers,
            patch("devstack.dashboard_state.published_ports") as ports,
            patch("devstack.dashboard_state.load_tasks", return_value=()),
            patch("devstack.dashboard_state.recent_jobs", return_value=[]),
            patch("devstack.dashboard_state.current_job", return_value=None),
            patch("devstack.dashboard_state.load_webhooks",
                  return_value=(Webhook("stripe", "API", "/hook"),)),
        ):
            data = dashboard.overview(project)
        self.assertEqual(data["ports"], [])
        self.assertEqual(data["containers"], [])
        self.assertEqual(data["services"], [])
        self.assertEqual(data["applications"][0]["state"], "inactive")
        self.assertEqual(data["webhooks"][0]["enabled"], False)
        for obj in (services, containers, ports):
            obj.assert_not_called()

    def test_registry_failure_does_not_hide_independent_sections(self):
        project = Project("demo", ())
        with (
            patch("devstack.dashboard_state.runtime.status",
                  return_value={"phase": "active", "project": "demo"}),
            patch("devstack.dashboard_state.load_applications",
                  side_effect=ApplicationError("Registry unavailable")),
            patch("devstack.dashboard_state.compose_states", return_value={}),
            patch("devstack.dashboard_state.compose_containers", return_value=()),
            patch("devstack.dashboard_state.published_ports", return_value=()),
            patch("devstack.dashboard_state.load_tasks", return_value=()),
            patch("devstack.dashboard_state.recent_jobs", return_value=[]),
            patch("devstack.dashboard_state.current_job", return_value=None),
            patch("devstack.dashboard_state.load_webhooks",
                  return_value=(Webhook("deploy", "API", "/deploy"),)),
        ):
            data = dashboard.overview(project)
        self.assertEqual(data["applications"], [])
        self.assertIn("applications", data["errors"])
        self.assertEqual(data["webhooks"][0]["name"], "deploy")


class ContainerInventoryTests(unittest.TestCase):
    @patch("devstack.container_inventory.compose_path")
    @patch("devstack.container_inventory.compose_command",
           return_value=["docker", "compose", "-p", "demo"])
    @patch("devstack.container_inventory.project_root", return_value=Path("/tmp"))
    @patch("devstack.container_inventory.subprocess.run")
    def test_only_structured_fields_returned(self, run, root, command, compose):
        import json
        compose.return_value.is_file.return_value = True
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps([
            {"Name": "api-1", "Service": "api", "State": "running",
             "Health": "healthy", "Environment": {"SECRET": "hidden"}},
            {"Name": "postgres-1", "Service": "postgres", "State": "exited",
             "Health": None},
        ]), "")
        items = compose_containers("demo")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["service"], "api")
        self.assertEqual(items[1]["state"], "exited")
        self.assertNotIn("Environment", items[0])
        self.assertEqual(run.call_count, 1)


class DashboardAPIContractTests(unittest.TestCase):
    def setUp(self):
        from devstack.web import create_app
        self.client = create_app({"TESTING": True, "SECRET_KEY": "test"}).test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    @patch("devstack.web.dashboard_state.overview")
    @patch("devstack.web.load_projects", return_value={"demo": Project("demo", ())})
    def test_dashboard_reads_single_workspace_snapshot(self, projects, overview):
        overview.return_value = {"schema_version": 1, "runtime": {"phase": "active"}}
        response = self.client.get("/api/v1/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["schema_version"], 1)
        overview.assert_called_once_with(Project("demo", ()))

    @patch("devstack.web.dashboard_state.overview")
    @patch("devstack.web.load_projects", return_value={})
    def test_empty_snapshot_does_not_create_project(self, projects, overview):
        overview.return_value = {"schema_version": 1, "project": None}
        response = self.client.get("/api/v1/dashboard")
        self.assertIsNone(response.json["project"])
        overview.assert_called_once_with(None)

    @patch("devstack.web.runtime.request_transition",
           return_value={"phase": "activating", "project": "demo"})
    @patch("devstack.web.load_projects", return_value={"demo": Project("demo", ())})
    def test_runtime_json_action_requires_csrf_header(self, projects, transition):
        target = "/api/v1/runtime/activate"
        self.assertEqual(self.client.post(target).status_code, 403)
        self.assertEqual(self.client.post(target, data={"_csrf": self.csrf}).status_code, 403)
        self.assertEqual(self.client.post(target, headers={"X-CSRF-Token": "wrong"}).status_code, 403)
        transition.assert_not_called()
        response = self.client.post(target, headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json["ok"])
        self.assertEqual(response.json["runtime"]["phase"], "activating")
        transition.assert_called_once_with("activate")

    @patch("devstack.web.runtime.request_transition",
           side_effect=__import__("devstack.runtime", fromlist=["RuntimeControlError"]).RuntimeControlError("already active"))
    @patch("devstack.web.load_projects", return_value={"demo": Project("demo", ())})
    def test_runtime_conflict_is_json_not_html(self, projects, transition):
        response = self.client.post("/api/v1/runtime/activate",
                                    headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["message"], "already active")

    @patch("devstack.web.load_projects", return_value={})
    def test_unconfigured_runtime_action_fails(self, projects):
        response = self.client.post("/api/v1/runtime/activate",
                                    headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 404)

    def test_dashboard_has_packaged_module_assets_and_mobile_view(self):
        with patch("devstack.web.load_projects", return_value={"demo": Project("demo", ())}):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'dashboard.js', response.data)
        self.assertIn(b'/api/v1/dashboard', self.client.get("/static/js/dashboard.js").data)
        self.assertIn(b'dashboard-containers', response.data)
        self.assertIn(b'dashboard-ports', response.data)
        self.assertEqual(self.client.get("/static/css/dashboard.css").status_code, 200)


if __name__ == "__main__":
    unittest.main()
