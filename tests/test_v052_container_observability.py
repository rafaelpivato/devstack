"""v0.5.2: project-/generation-scoped Docker service observability."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from devstack.api.client import ControllerRejected
from devstack.api.http import create_app
from devstack.control.daemon import Controller, ProtocolError
from devstack.control.docker import DockerBackend, DockerControlError
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database


class FakeDocker:
    def __init__(self):
        self.running = []
        self.observations = []
        self.log_reads = []

    def owned(self):
        return list(self.running)

    def validate(self, _identifier):
        pass

    def start(self, identifier, generation):
        self.running = [{
            "id": "1" * 64, "project": identifier,
            "generation": generation, "state": "running",
        }]

    def verify(self, identifier, generation):
        assert (len(self.running) == 1 and
                self.running[0]["project"] == identifier and
                self.running[0]["generation"] == generation)

    def stop(self, identifier):
        self.running = []

    def service_snapshot(self, identifier, generation):
        self.observations.append((identifier, generation))
        return {"services": [
            {"name": "web", "status": "running",
             "containers": [{"id": "1" * 64, "name": "web-1",
                             "state": "running", "health": "healthy",
                             "restart_count": 0}]},
        ]}

    def service_logs(self, identifier, generation, container_id, tail):
        self.log_reads.append((identifier, generation, container_id, tail))
        if container_id != "1" * 64:
            raise DockerControlError("Wrong container")
        return {"service": "web", "container_id": container_id,
                "tail": tail, "text": "Django started", "truncated": False}


class ContainerObservationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.backend = FakeDocker()
        self.controller = Controller(
            Database(root / "db.sqlite3"),
            WorkspaceStore(root / "projects"), self.backend,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "Alpha").id
        self.b = self.controller.provisioner.create_empty("beta", "Beta").id

        def transport(operation, **args):
            try:
                return self.controller.handle({"op": operation, **args})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc

        self.browser = create_app({
            "TESTING": True, "SECRET_KEY": "test", "SESSION_COOKIE_SECURE": False,
        }, transport=transport).test_client()

    def test_inactive_selection_never_observes_or_activates(self):
        self.assertEqual(self.controller.observability.services(self.a), {
            "project_id": self.a, "active": False, "services": [],
        })
        page = self.browser.get("/projects/" + self.a)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"devstack-csrf", page.data)
        self.assertEqual(self.browser.get(
            "/api/v2/projects/" + self.a + "/services",
        ).get_json()["services"], [])
        self.assertEqual(self.backend.observations, [])
        self.assertEqual(self.backend.log_reads, [])
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")

    def test_active_isolation_and_logs_without_running_commands(self):
        active = self.controller.coordinator.activate(self.a)
        result = self.browser.get(
            "/api/v2/projects/" + self.a + "/services",
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["services"][0]["containers"][0]["health"],
                         "healthy")
        self.assertEqual(self.backend.observations[-1],
                         (self.a, active["generation"]))
        self.assertEqual(self.browser.get("/projects/" + self.a + "/services").status_code, 200)
        self.assertEqual(self.browser.get(
            "/api/v2/projects/" + self.b + "/services",
        ).get_json()["services"], [])
        self.assertEqual(self.browser.get(
            "/api/v2/projects/" + self.b + "/services/" + "1"*64 + "/logs",
        ).status_code, 409)
        self.assertEqual(self.browser.get(
            "/api/v2/projects/" + self.a + "/services/" + "1"*64 + "/logs",
        ).get_json()["text"], "Django started")
        self.assertEqual(self.backend.log_reads[-1][-1], 150)
        self.assertEqual(self.browser.get(
            "/projects/" + self.a + "/services/" + "1"*64,
        ).status_code, 200)
        self.assertEqual(self.browser.get(
            "/api/v2/projects/" + str(uuid4()) + "/services",
        ).status_code, 404)

    def test_logs_are_read_only_and_tail_is_bounded(self):
        self.controller.coordinator.activate(self.a)
        url = "/api/v2/projects/" + self.a + "/services/" + "1"*64 + "/logs"
        for value in ("0", "-1", "201", "oops", "999999999"):
            with self.subTest(value=value):
                self.assertEqual(self.browser.get(url + "?tail=" + value).status_code, 400)
        self.assertEqual(self.browser.get(url + "?tail=200").status_code, 200)
        self.assertEqual(self.browser.post(url).status_code, 403)
        self.assertEqual(self.browser.post(
            "/api/v2/projects/" + self.a + "/services",
        ).status_code, 403)
        with self.assertRaises(ProtocolError):
            self.controller.handle({
                "op": "service_logs", "project_id": self.a,
                "container_id": "1"*64, "tail": 150, "docker_command": "exec",
            })

    def test_project_switch_invalidates_observation_authority(self):
        original = self.controller.coordinator.activate(self.a)
        self.controller.coordinator.switch(self.b)
        with self.assertRaises(Exception):
            self.controller.observability.logs(self.a, "1"*64, 100)
        result = self.controller.observability.services(self.a)
        self.assertFalse(result["active"])
        self.assertEqual(self.backend.observations, [])
        self.assertNotEqual(
            self.controller.coordinator.status()["generation"],
            original["generation"],
        )


class DockerObservationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.project = str(uuid4())
        self.generation = "d" * 32
        self.ident = "a" * 64

    def _container(self, *, project=None, generation=None, service="web",
                   oneoff=False, health="healthy"):
        p = project or self.project
        labels = {
            "devstack.project_id": p,
            "devstack.generation": generation or self.generation,
            "com.docker.compose.project": "devstack-" + p,
            "com.docker.compose.service": service,
        }
        if oneoff:
            labels["com.docker.compose.oneoff"] = "True"
            labels["devstack.job_id"] = str(uuid4())
        return {"Id": self.ident, "Name": "/web-1", "Config": {"Labels": labels},
                "State": {"Status": "running", "Health": {"Status": health}},
                "RestartCount": 2}

    def test_docker_identity_enforced_for_status_and_logs(self):
        valid = self._container()
        with patch("devstack.control.docker._execute",
                   return_value=json.dumps([valid])):
            record = DockerBackend._checked_service_container(
                self.project, self.generation, self.ident,
            )
        self.assertEqual(record["Id"], self.ident)
        for record in (
            self._container(project=str(uuid4())),
            self._container(generation="f" * 32),
            self._container(oneoff=True),
            self._container(service="bad service"),
        ):
            with self.subTest(record=record["Config"]["Labels"]), patch(
                "devstack.control.docker._execute",
                return_value=json.dumps([record]),
            ), self.assertRaises(DockerControlError):
                DockerBackend._checked_service_container(
                    self.project, self.generation, self.ident,
                )
        with self.assertRaises(DockerControlError):
            DockerBackend._checked_service_container(
                self.project, self.generation, "not-a-real-container-id",
            )

    def test_snapshot_excludes_oneoff_and_includes_missing_services(self):
        backend = object.__new__(DockerBackend)
        backend.command = lambda _: ["docker", "compose", "-p", "devstack-" + self.project]
        second = "b"*64
        oneoff = self._container(oneoff=True)
        oneoff["Id"] = second
        def execute(command, timeout=240):
            if command[-2:] == ["config", "--services"]:
                return "web\npostgres"
            if command[:3] == ["docker", "ps", "--all"]:
                return self.ident + "\n" + second
            if command[:3] == ["docker", "inspect", "--type"]:
                return json.dumps([valid if command[-1] == self.ident else oneoff])
            raise AssertionError(command)
        valid = self._container()
        with patch("devstack.control.docker._execute", side_effect=execute):
            snapshot = backend.service_snapshot(self.project, self.generation)
        self.assertEqual(
            [service["status"] for service in snapshot["services"]],
            ["running", "not-created"],
        )
        self.assertEqual(snapshot["services"][0]["containers"][0]["health"], "healthy")
        self.assertEqual(snapshot["services"][0]["containers"][0]["restart_count"], 2)
        self.assertEqual(snapshot["services"][1]["containers"], [])

    def test_logs_do_not_reach_unowned_container(self):
        backend = object.__new__(DockerBackend)
        with patch("devstack.control.docker._execute",
                   return_value=json.dumps([self._container(project=str(uuid4()))])), patch(
                   "devstack.control.docker._tail_logs") as logs:
            with self.assertRaises(DockerControlError):
                backend.service_logs(self.project, self.generation, self.ident, 50)
            logs.assert_not_called()
        with patch("devstack.control.docker._execute",
                   return_value=json.dumps([self._container()])), patch(
                   "devstack.control.docker._tail_logs",
                   return_value={"tail": 50, "text": "ready", "truncated": False}) as logs:
            result = backend.service_logs(self.project, self.generation, self.ident, 50)
            self.assertEqual(result["text"], "ready")
            logs.assert_called_once_with(self.ident, 50)

if __name__ == "__main__":
    unittest.main()
