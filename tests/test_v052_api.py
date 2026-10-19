"""ADR-0001 Stage 4: v2 HTTP, browser selection and project isolation.

Tests drive Flask through the same typed controller operation boundary that
the production Unix-socket client exposes; SQLite and Docker are never
accessed directly by HTTP handlers.
"""
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from devstack.api.client import ControllerRejected, ControllerUnavailable
from devstack.api.http import create_app
from devstack.control.daemon import Controller
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "smoke"
URL = "https://github.com/rafaelpivato/devstack/tree/main/examples/smoke"


class FakeGitHub:
    def clone_and_resolve(self, url, destination):
        from devstack.adapters.github import GitHubCheckout
        destination.mkdir()
        example = destination / "examples" / "smoke"
        example.parent.mkdir()
        shutil.copytree(SAMPLE, example)
        return GitHubCheckout(
            "https://github.com/rafaelpivato/devstack.git",
            "devstack", "main", "examples/smoke", example,
        )


class FakeBackend:
    def __init__(self):
        self.containers = []
        self.events = []

    def validate(self, identifier):
        self.events.append(("validate", identifier))

    def owned(self):
        return list(self.containers)

    def start(self, identifier, generation):
        self.events.append(("start", identifier))
        self.containers.append({
            "id": str(uuid4()), "project": identifier,
            "generation": generation, "state": "running",
        })

    def verify(self, identifier, generation):
        assert any(p["project"] == identifier and p["generation"] == generation
                   for p in self.containers)

    def stop(self, identifier):
        self.events.append(("stop", identifier))
        self.containers = [item for item in self.containers
                           if item["project"] != identifier]


class HttpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        backend = FakeBackend()
        self.backend = backend
        self.controller = Controller(
            Database(base / "state" / "control.sqlite3"),
            WorkspaceStore(base / "workspaces"), backend,
        )
        self.controller.provisioner.github = FakeGitHub()
        self.controller.coordinator.reconcile()
        self.calls = []

        def transport(op, **args):
            self.calls.append((op, dict(args)))
            try:
                return self.controller.handle({"op": op, **args})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc

        self.app = create_app({
            "TESTING": True, "SECRET_KEY": "test-only-secret",
            "SESSION_COOKIE_SECURE": False,
        }, transport=transport)
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf"]

    def post(self, path, **data):
        return self.client.post(path, data={"_csrf": self.csrf, **data})

    def api_post(self, path, body=None, csrf=True):
        return self.client.post(path, json=body or {},
                                headers={"X-CSRF-Token": self.csrf} if csrf else {})

    def create(self, slug):
        result = self.api_post("/api/v2/projects", {
            "slug": slug, "name": slug.upper(),
        })
        self.assertEqual(result.status_code, 201, result.get_json())
        return result.get_json()["project"]["id"]

    def test_many_projects_created_and_browsed_without_activation(self):
        a, b = self.create("alpha"), self.create("beta")
        response = self.client.get("/projects/" + b)
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("ALPHA", body)
        self.assertIn("BETA", body)
        self.assertIn('aria-current="page"', body)
        self.assertIn("Viewing this project does not activate it", body)
        self.assertEqual(self.client.get("/api/v2/runtime").get_json()["phase"],
                         "inactive")
        self.assertEqual(self.backend.containers, [])
        self.assertEqual(len(self.client.get("/api/v2/projects").get_json()["projects"]), 2)
        self.assertFalse(any(op in ("activate", "switch") for op, _ in self.calls))

    def test_switching_and_browser_selection_are_independent(self):
        a, b = self.create("alpha"), self.create("beta")
        first = self.api_post("/api/v2/projects/" + a + "/activate")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["runtime"]["project_id"], a)
        selection = self.client.get("/projects/" + b)
        self.assertEqual(selection.status_code, 200)
        self.assertEqual(self.client.get("/api/v2/runtime").get_json()["project_id"], a)
        self.assertNotIn(("start", b), self.backend.events)
        self.assertIn("Switch runtime to this project", selection.get_data(as_text=True))
        switched = self.api_post("/api/v2/runtime/switch", {"project_id": b})
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(switched.get_json()["runtime"]["project_id"], b)
        self.assertEqual(self.backend.events[-2:], [("stop", a), ("start", b)])
        self.assertEqual(len(self.backend.containers), 1)
        self.assertEqual(self.api_post("/api/v2/runtime/deactivate").status_code, 200)
        self.assertEqual(self.backend.containers, [])

    def test_inactive_project_can_be_renamed_without_switching(self):
        a, b = self.create("alpha"), self.create("beta")
        self.api_post("/api/v2/projects/" + a + "/activate")
        response = self.post("/projects/" + b + "/rename", name="Renamed B")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.controller.registry.get(b).name, "Renamed B")
        self.assertEqual(self.controller.coordinator.status()["project_id"], a)
        self.assertNotIn(("start", b), self.backend.events)
        patched = self.client.patch(
            "/api/v2/projects/" + b, json={"name": "B Again"},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(self.controller.registry.get(b).name, "B Again")

    def test_import_stays_available_after_first_project(self):
        a = self.create("alpha")
        before = self.controller.coordinator.status()
        sample = self.client.get("/?example=smoke")
        self.assertEqual(sample.status_code, 200)
        self.assertIn(URL, sample.get_data(as_text=True))
        self.assertEqual(len(self.controller.registry.list()), 1)
        imported = self.api_post("/api/v2/projects/import", {
            "url": URL, "slug": "smoke", "name": "Smoke Test",
        })
        self.assertEqual(imported.status_code, 201, imported.get_json())
        b = imported.get_json()["project"]["id"]
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.controller.registry.list()), 2)
        self.assertEqual(self.controller.coordinator.status(), before)
        detail = self.client.get("/api/v2/projects/" + b).get_json()
        self.assertEqual(detail["applications"][0]["name"], "Web")
        self.assertIn("tests", [t["name"] for t in detail["tasks"]])
        self.assertEqual(self.client.get("/projects/" + b).status_code, 200)

    def test_inactive_task_rejected_and_run_control_absent(self):
        a = self.create("alpha")
        imported = self.api_post("/api/v2/projects/import", {
            "url": URL, "slug": "smoke", "name": "Smoke Test",
        })
        b = imported.get_json()["project"]["id"]
        self.api_post("/api/v2/projects/" + a + "/activate")
        page = self.client.get("/projects/" + b).get_data(as_text=True)
        self.assertIn("Inactive · cannot run", page)
        self.assertNotIn("Run</button>", page)
        result = self.api_post("/api/v2/projects/" + b + "/tasks/tests/run")
        self.assertEqual(result.status_code, 409, result.get_json())
        self.assertEqual(self.controller.coordinator.status()["project_id"], a)

    def test_unknown_or_wrong_project_requests_do_not_mutate_active(self):
        a, b = self.create("alpha"), self.create("beta")
        self.api_post("/api/v2/projects/" + a + "/activate")
        unknown = str(uuid4())
        self.assertEqual(self.client.get("/api/v2/projects/" + unknown).status_code, 404)
        self.assertEqual(self.client.get("/projects/" + unknown).status_code, 404)
        self.assertEqual(
            self.api_post("/api/v2/projects/" + unknown + "/activate").status_code, 404
        )
        self.assertEqual(self.controller.coordinator.status()["project_id"], a)

    def test_csrf_on_every_mutation_including_json_and_html(self):
        a = self.create("alpha")
        self.assertEqual(self.client.post("/api/v2/projects",
                                           json={"slug": "bad", "name": "Bad"}).status_code, 403)
        self.assertEqual(self.client.patch(
            "/api/v2/projects/" + a, json={"name": "Bad"},
        ).status_code, 403)
        self.assertEqual(self.client.post(
            "/projects/" + a + "/activate", data={},
        ).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/v2/runtime/switch", json={"project_id": a},
        ).status_code, 403)
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")
        self.assertEqual(len(self.controller.registry.list()), 1)

    def test_json_validation_and_no_implicit_current_project(self):
        a = self.create("alpha")
        self.assertEqual(self.api_post(
            "/api/v2/projects", {"slug": "beta"}, csrf=True,
        ).status_code, 400)
        self.assertEqual(self.client.post("/api/v2/projects", data={
            "_csrf": self.csrf, "slug": "beta", "name": "Beta",
        }).status_code, 415)
        self.assertEqual(self.api_post(
            "/api/v2/projects/" + a + "/compose/down",
        ).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/project").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/dashboard").status_code, 404)
        self.assertEqual(self.client.get("/api/v2/runtime").get_json()["project_id"],
                         None)

    def test_controller_unavailability_fails_closed(self):
        def disconnected(op, **kwargs):
            raise ControllerUnavailable("no controller")
        app = create_app({"TESTING": True, "SECRET_KEY": "secret",
                          "SESSION_COOKIE_SECURE": False}, transport=disconnected)
        client = app.test_client()
        self.assertEqual(client.get("/api/v2/runtime").status_code, 503)
        self.assertEqual(client.get("/healthz").status_code, 503)
        self.assertEqual(client.get("/").status_code, 503)

    def test_project_job_lookup_is_scoped(self):
        a, b = self.create("alpha"), self.create("beta")
        fake_id = str(uuid4())
        with self.controller.registry.database.transaction(write=True) as con:
            con.execute(
                "INSERT INTO jobs(id,project_id,generation,kind,status,"
                "action,started_ns) VALUES (?,?,?,'task','succeeded','example',?)",
                (fake_id, a, "gen", time.time_ns()),
            )
        self.assertEqual(self.client.get(
            "/api/v2/projects/" + a + "/jobs/" + fake_id,
        ).status_code, 200)
        self.assertEqual(self.client.get(
            "/api/v2/projects/" + b + "/jobs/" + fake_id,
        ).status_code, 422)


if __name__ == "__main__":
    unittest.main()
