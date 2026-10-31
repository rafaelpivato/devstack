"""v0.5.2: verified, private, direct Tailscale Docker port exposure."""
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
from devstack.control.private_ports import PrivateApplicationPorts, tailscale_ipv4
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database


IP = "100.98.76.54"


class FakeDocker:
    def __init__(self):
        self.containers = []
        self.lookups = []

    def owned(self):
        return list(self.containers)

    def validate(self, identifier):
        return None

    def start(self, identifier, generation):
        self.containers = [{"id": "a"*64, "project": identifier,
                            "generation": generation, "state": "running"}]

    def stop(self, identifier):
        self.containers.clear()

    def verify(self, identifier, generation):
        assert (len(self.containers) == 1 and
                self.containers[0]["project"] == identifier and
                self.containers[0]["generation"] == generation)

    def private_published_ports(self, identifier, generation, host):
        self.lookups.append((identifier, generation, host))
        return [{"service": "web", "container_port": 8000, "host_port": 18080,
                 "url": f"http://{host}:18080/"},
                {"service": "web", "container_port": 8001, "host_port": 18081,
                 "url": f"http://{host}:18081/"},
                {"service": "worker", "container_port": 5678, "host_port": 15678,
                 "url": f"http://{host}:15678/"}]


class PrivatePortsAPITests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.backend = FakeDocker()
        self.controller = Controller(
            Database(root / "db.sqlite3"),
            WorkspaceStore(root / "projects"), self.backend,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "Alpha").id
        self.b = self.controller.provisioner.create_empty("beta", "Beta").id
        with self.controller.registry.database.transaction(write=True) as db:
            db.execute(
                "INSERT INTO applications(project_id,name,service,port) VALUES(?,?,?,?)",
                (self.a, "Django", "web", 8000),
            )
            db.execute(
                "INSERT INTO applications(project_id,name,service,port) VALUES(?,?,?,?)",
                (self.a, "Other web endpoint", "web", None),
            )
            db.execute(
                "INSERT INTO applications(project_id,name,service,port) VALUES(?,?,?,?)",
                (self.a, "Missing", "missing", 8000),
            )

        def transport(op, **kwargs):
            try:
                return self.controller.handle({"op": op, **kwargs})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc
        self.browser = create_app(
            {"TESTING": True, "SECRET_KEY": "test", "SESSION_COOKIE_SECURE": False},
            transport=transport,
        ).test_client()

    def test_inactive_project_never_queries_tailscale_or_docker(self):
        with patch("devstack.control.private_ports.tailscale_ipv4",
                   side_effect=AssertionError("No Tailscale lookup expected")):
            result = self.controller.private_ports.list(self.a)
            self.assertFalse(result["active"])
            self.assertEqual(result["ports"], [])
            self.assertEqual(self.browser.get(
                "/api/v2/projects/" + self.a + "/private-ports",
            ).status_code, 200)
        self.assertFalse(self.backend.lookups)
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")

    def test_active_project_only_and_optional_application_labels(self):
        activated = self.controller.coordinator.activate(self.a)
        with patch("devstack.control.private_ports.tailscale_ipv4", return_value=IP):
            response = self.browser.get(
                "/api/v2/projects/" + self.a + "/private-ports",
            )
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data["project_id"], self.a)
            self.assertTrue(data["tailscale_available"])
            self.assertEqual(len(data["ports"]), 3)
            # Discovered bindings remain diagnostic-only; application URLs
            # come from explicit project configuration, not port matching.
            self.assertEqual(data["applications"], [])
            self.assertEqual(self.backend.lookups[-1],
                             (self.a, activated["generation"], IP))
            # Neither selecting nor querying B accesses active A's ports.
            wrong = self.browser.get(
                "/api/v2/projects/" + self.b + "/private-ports",
            )
            self.assertEqual(wrong.status_code, 200)
            self.assertFalse(wrong.get_json()["active"])
            self.assertEqual(wrong.get_json()["ports"], [])
            self.assertEqual(len(self.backend.lookups), 1)
            self.assertEqual(self.browser.get("/projects/" + self.a + "/applications").status_code, 200)

    def test_missing_tailscale_yields_no_links_and_no_docker_queries(self):
        self.controller.coordinator.activate(self.a)
        with patch("devstack.control.private_ports.tailscale_ipv4", return_value=None):
            result = self.controller.private_ports.list(self.a)
        self.assertTrue(result["active"])
        self.assertFalse(result["tailscale_available"])
        self.assertEqual(result["ports"], [])
        self.assertEqual(result["applications"], [])
        self.assertEqual(self.backend.lookups, [])

    def test_switch_invalidates_old_links_without_restarting_other_project(self):
        self.controller.coordinator.activate(self.a)
        self.controller.coordinator.switch(self.b)
        with patch("devstack.control.private_ports.tailscale_ipv4", return_value=IP):
            self.assertFalse(self.controller.private_ports.list(self.a)["active"])
            self.assertEqual(self.controller.private_ports.list(self.b)["project_id"],
                             self.b)
        self.assertEqual(len(self.backend.lookups), 1)
        self.assertEqual(self.backend.lookups[0][0], self.b)

    def test_only_read_only_allowlisted_api_exists(self):
        self.controller.coordinator.activate(self.a)
        url = "/api/v2/projects/" + self.a + "/private-ports"
        self.assertEqual(self.browser.post(url).status_code, 403)
        with self.assertRaises(ProtocolError):
            self.controller.handle({"op": "private_ports",
                                    "project_id": self.a, "bind": "0.0.0.0"})
        self.assertEqual(
            self.browser.get("/api/v2/projects/" + str(uuid4()) +
                             "/private-ports").status_code, 404,
        )


class TailscaleIPTests(unittest.TestCase):
    def test_exact_cgnat_address_required(self):
        from subprocess import CompletedProcess
        for output in ("100.64.0.1", "100.127.255.254"):
            with patch("devstack.control.private_ports.subprocess.run",
                       return_value=CompletedProcess(["tailscale"], 0, output, "")):
                self.assertEqual(tailscale_ipv4(), output)
        for output in ("0.0.0.0", "127.0.0.1", "192.168.1.5",
                       "8.8.8.8", "100.128.0.1", "100.64.0.1\n127.0.0.1", ""):
            with self.subTest(output=output), patch(
                "devstack.control.private_ports.subprocess.run",
                return_value=CompletedProcess(["tailscale"], 0, output, ""),
            ):
                self.assertIsNone(tailscale_ipv4())


class PrivateDockerBindingTests(unittest.TestCase):
    def setUp(self):
        self.project = str(uuid4())
        self.generation = "a"*32
        self.container = "a"*64
        self.backend = object.__new__(DockerBackend)
        self.host = IP

    def _record(self, *, project=None, generation=None, ports=None,
                running=True, oneoff=False):
        ident = project or self.project
        labels = {"devstack.project_id": ident,
                  "devstack.generation": generation or self.generation,
                  "com.docker.compose.project": "devstack-" + ident,
                  "com.docker.compose.service": "web"}
        if oneoff:
            labels["com.docker.compose.oneoff"] = "True"
            labels["devstack.job_id"] = str(uuid4())
        return {
            "Id": self.container, "Name": "/web-1",
            "Config": {"Labels": labels},
            "State": {"Status": "running" if running else "exited",
                      "Running": running},
            "NetworkSettings": {"Ports": ports or {}},
        }

    def _run(self, record):
        self.backend.service_snapshot = lambda a, b: {
            "services": [{"name": "web", "status": "running",
                          "containers": [{"id": self.container,
                                          "name": "web-1",
                                          "state": "running"}]}],
        }
        with patch("devstack.control.docker._execute", return_value=json.dumps([record])):
            return self.backend.private_published_ports(
                self.project, self.generation, self.host,
            )

    def test_only_exact_tailscale_host_tcp_mapping_is_advertised(self):
        record = self._record(ports={
            "8000/tcp": [
                {"HostIp": IP, "HostPort": "18080"},
                {"HostIp": "0.0.0.0", "HostPort": "18080"},
                {"HostIp": "127.0.0.1", "HostPort": "18080"},
                {"HostIp": "::", "HostPort": "18080"},
                {"HostIp": "100.98.76.55", "HostPort": "18080"},
            ],
            "5432/udp": [{"HostIp": IP, "HostPort": "15432"}],
        })
        self.assertEqual(self._run(record), [{
            "service": "web", "container_port": 8000, "host_port": 18080,
            "url": f"http://{IP}:18080/",
        }])

    def test_no_wildcard_loopback_udp_or_stopped_ports(self):
        mixed = {
            "8000/tcp": [
                {"HostIp": "0.0.0.0", "HostPort": "18080"},
                {"HostIp": "127.0.0.1", "HostPort": "18080"},
            ],
            "7000/udp": [{"HostIp": IP, "HostPort": "17000"}],
        }
        self.assertEqual(self._run(self._record(ports=mixed)), [])
        self.assertEqual(self._run(self._record(
            running=False,
            ports={"8000/tcp": [{"HostIp": IP, "HostPort": "18080"}]},
        )), [])

    def test_malformed_and_foreign_containers_never_produce_links(self):
        for record in (self._record(project=str(uuid4())),
                       self._record(generation="f"*32),
                       self._record(oneoff=True)):
            with self.subTest(labels=record["Config"]["Labels"]):
                with self.assertRaises(DockerControlError):
                    self._run(record)
        for host in ("0.0.0.0", "127.0.0.1", "10.0.0.1", "192.168.1.1",
                     "100.128.0.1", "::1"):
            with self.subTest(host=host), self.assertRaises(DockerControlError):
                self.backend.private_published_ports(
                    self.project, self.generation, host,
                )

    def test_malformed_port_numbers_are_skipped(self):
        record = self._record(ports={
            "8000/tcp": [{"HostIp": IP, "HostPort": "0"},
                         {"HostIp": IP, "HostPort": "65536"},
                         {"HostIp": IP, "HostPort": "abc"}],
            "weird": [{"HostIp": IP, "HostPort": "1234"}],
        })
        self.assertEqual(self._run(record), [])

if __name__ == "__main__":
    unittest.main()
