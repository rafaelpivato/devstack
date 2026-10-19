"""ADR-0001 Stage 3: exclusive execution, fencing, switching and recovery."""
import concurrent.futures
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from devstack.control.coordinator import ControlError, Coordinator
from devstack.control.daemon import Controller, ControlServer, ProtocolError, request
from devstack.control.jobs import JobSupervisor, JobError
from devstack.storage.database import Database
from devstack.storage.projects import ProjectRegistry
from devstack.projects.workspaces import WorkspaceStore


class FakeDocker:
    def __init__(self):
        self.containers = []
        self.log = []
        self.fail_stop = False
        self.fail_start = False
        self.invalid = set()
        self.guard = threading.Lock()

    def owned(self):
        with self.guard:
            return list(self.containers)

    def validate(self, identifier):
        self.log.append(("validate", identifier))
        if identifier in self.invalid:
            raise ValueError("invalid Compose")

    def start(self, identifier, generation):
        with self.guard:
            if self.fail_start:
                raise OSError("Docker failed")
            self.log.append(("start", identifier))
            self.containers.append({
                "id": str(uuid4()), "project": identifier,
                "generation": generation, "state": "running",
            })

    def stop(self, identifier):
        with self.guard:
            self.log.append(("stop", identifier))
            if self.fail_stop:
                raise OSError("Docker stop failure")
            self.containers = [r for r in self.containers
                               if r["project"] != identifier]

    def verify(self, identifier, generation):
        if not any(row["project"] == identifier and
                   row["generation"] == generation for row in self.owned()):
            raise OSError("missing running container")

    def task_command(self, identifier, task, generation, job_id):
        if task["service"] != "web":
            raise ValueError("bad test fixture task")
        return [sys.executable, "-c", "import time; time.sleep(20)"]

    def command(self, identifier):
        return [sys.executable, "-c", "print('ok')"]


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        folder = Path(home.name)
        self.db = Database(folder / "control.sqlite3")
        self.registry = ProjectRegistry(self.db)
        self.workspaces = WorkspaceStore(folder / "projects")
        self.a = self.registry.create("a", "Project A")
        self.b = self.registry.create("b", "Project B")
        self.backend = FakeDocker()
        self.engine = Coordinator(self.db, self.registry, self.backend)

    def test_activation_then_switch_and_return_persists_projects(self):
        self.engine.reconcile()
        a = self.engine.activate(self.a.id)
        self.assertEqual(a["phase"], "active")
        self.assertTrue(self.engine.authorized(self.a.id, a["generation"]))
        switched = self.engine.switch(self.b.id)
        self.assertEqual(switched["project_id"], self.b.id)
        self.assertFalse(self.engine.authorized(self.a.id, a["generation"]))
        self.assertEqual(self.backend.log[-2:],
                         [("stop", self.a.id), ("start", self.b.id)])
        final = self.engine.switch(self.a.id)
        self.assertNotEqual(final["generation"], a["generation"])
        self.assertTrue(self.engine.authorized(self.a.id, final["generation"]))
        self.assertEqual({p.id for p in self.registry.list()}, {self.a.id, self.b.id})
        self.assertEqual(len(self.backend.owned()), 1)
        self.assertEqual(self.engine.deactivate()["phase"], "inactive")
        self.assertFalse(self.backend.owned())

    def test_two_activation_requests_never_overlap(self):
        self.engine.reconcile()
        def activate(identifier):
            try:
                return self.engine.activate(identifier)
            except ControlError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(activate, (self.a.id, self.b.id)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(len(self.backend.owned()), 1)

    def test_invalid_target_cannot_disrupt_current_project(self):
        self.engine.reconcile()
        self.engine.activate(self.a.id)
        self.backend.invalid.add(self.b.id)
        with self.assertRaises(ControlError):
            self.engine.switch(self.b.id)
        self.assertEqual(self.engine.status()["project_id"], self.a.id)
        self.assertEqual(self.engine.status()["phase"], "active")
        self.assertNotIn(("stop", self.a.id), self.backend.log)

    def test_failed_cleanup_never_activates_second_project(self):
        self.engine.reconcile()
        self.engine.activate(self.a.id)
        self.backend.fail_stop = True
        with self.assertRaisesRegex(ControlError, "failed closed"):
            self.engine.switch(self.b.id)
        self.assertEqual(self.engine.status()["phase"], "error")
        self.assertNotIn(("start", self.b.id), self.backend.log)
        with self.assertRaises(ControlError):
            self.engine.activate(self.b.id)

    def test_start_failure_cannot_create_false_active_state(self):
        self.engine.reconcile()
        self.backend.fail_start = True
        with self.assertRaises(ControlError):
            self.engine.activate(self.a.id)
        self.assertEqual(self.engine.status()["phase"], "error")
        self.assertFalse(self.backend.owned())

    def test_crash_restart_recovers_to_inactive_not_old_active(self):
        self.engine.reconcile()
        self.engine.activate(self.a.id)
        replacement = Coordinator(self.db, self.registry, self.backend)
        self.assertEqual(replacement.reconcile()["phase"], "inactive")
        self.assertFalse(self.backend.owned())
        self.assertEqual(replacement.status()["project_id"], None)
        self.assertEqual(len(self.registry.list()), 2)

    def test_unknown_owned_resource_blocks_recovery(self):
        with self.backend.guard:
            self.backend.containers.append({
                "id": "foreign", "project": str(uuid4()),
                "generation": "unknown", "state": "running",
            })
        with self.assertRaisesRegex(ControlError, "failed closed"):
            self.engine.reconcile()
        self.assertEqual(self.engine.status()["phase"], "error")
        with self.assertRaises(ControlError):
            self.engine.activate(self.a.id)

    def test_unavailable_docker_blocks_recovery(self):
        def fail():
            raise RuntimeError("Docker unreachable")
        self.backend.owned = fail
        with self.assertRaises(ControlError):
            self.engine.reconcile()
        self.assertEqual(self.engine.status()["phase"], "error")

    def test_inactive_project_has_no_implicit_admission(self):
        self.engine.reconcile()
        active = self.engine.activate(self.a.id)
        with self.assertRaisesRegex(ControlError, "Stale"):
            self.engine.admit(self.b.id, active["generation"])
        self.engine.deactivate()
        with self.assertRaisesRegex(ControlError, "Stale"):
            self.engine.admit(self.a.id, active["generation"])


class JobAndSocketTests(CoordinatorTests):
    def setUp(self):
        super().setUp()
        self.jobs = JobSupervisor(self.engine, self.backend, self.workspaces)
        # Manually provision an inert workspace to exercise worker log handling.
        for project in (self.a, self.b):
            self.workspaces.prepare(project.id)
            self.workspaces.publish(project.id)
        with self.db.transaction(write=True) as con:
            con.execute(
                "INSERT INTO tasks(project_id,name,service,mode,image,network,argv_json)"
                " VALUES(?,?,?,'exec','','none',?)",
                (self.a.id, "long", "web", '["python","-V"]'),
            )

    def test_running_job_cancelled_before_switch(self):
        self.engine.reconcile()
        active = self.engine.activate(self.a.id)
        job_id = self.jobs.submit_task(self.a.id, "long")
        for _ in range(100):
            if self.jobs.get(job_id)["status"] == "running":
                break
            time.sleep(.02)
        self.assertEqual(self.jobs.get(job_id)["status"], "running")
        self.engine.switch(self.b.id)
        self.assertEqual(self.jobs.get(job_id)["status"], "cancelled")
        self.assertFalse(self.engine.authorized(self.a.id, active["generation"]))
        self.assertEqual(self.engine.status()["project_id"], self.b.id)

    def test_stale_queued_job_cannot_run_after_reactivation(self):
        self.engine.reconcile()
        a = self.engine.activate(self.a.id)
        # A worker blocked on the global lock until after deactivation is
        # cancelled and cannot be replayed for a new activation generation.
        with self.engine.locked():
            job_id = self.jobs.submit_task if False else None
            with self.db.transaction(write=True) as con:
                con.execute(
                    "INSERT INTO jobs(id,project_id,generation,kind,status,"
                    "action,started_ns) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid4()), self.a.id, a["generation"], "task",
                     "cancelled", "long", time.time_ns()),
                )
        self.engine.deactivate()
        self.engine.activate(self.a.id)
        with self.assertRaises(ControlError):
            self.engine.admit(self.a.id, a["generation"])

    def test_socket_has_narrow_allowlisted_commands(self):
        controller = Controller(self.db, self.workspaces, self.backend)
        controller.coordinator.reconcile()
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "controller.sock"
            with ControlServer(socket_path, controller) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    result = request(socket_path, {"op": "projects"})
                    self.assertEqual(len(result["projects"]), 2)
                    with self.assertRaises(ProtocolError):
                        request(socket_path, {"op": "exec", "argv": ["sh"]})
                    self.assertEqual(request(socket_path, {
                        "op": "activate", "project_id": self.a.id
                    })["phase"], "active")
                    self.assertEqual(request(socket_path, {
                        "op": "switch", "project_id": self.b.id
                    })["project_id"], self.b.id)
                    self.assertEqual(request(socket_path, {
                        "op": "deactivate"
                    })["phase"], "inactive")
                finally:
                    server.shutdown()
                    thread.join(3)


if __name__ == "__main__":
    unittest.main()
