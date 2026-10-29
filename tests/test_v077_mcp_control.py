"""v0.7.7: MCP direct Unix socket and controller-owned writes.

MCP never starts a private Web server, grants new roles or executes host shell.
"""
from pathlib import Path
import tempfile
import threading
import unittest

from devstack.control.daemon import ControlServer
from devstack.mcp_reads import MCPReadClient, MCPReadError
from devstack.networking import PUBLIC_UNIT
from devstack.mcp_management import UNIT_FILES

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
REPO = "33333333-3333-4333-8333-333333333333"
JOB = "44444444-4444-4444-8444-444444444444"
OPERATION = "55555555-5555-4555-8555-555555555555"
HEAD = "a" * 40


class ControllerStub:
    def __init__(self):
        self.calls = []

    def handle(self, request):
        self.calls.append(request)
        op = request["op"]
        if op == "health":
            return {"alive": True, "runtime": "active"}
        if op == "projects":
            return {"projects": [{"id": A, "name": "Stitch"},
                                 {"id": B, "name": "Other"}]}
        if op == "status":
            return {"phase": "active", "project_id": A, "updated_ns": 2}
        if op == "project_detail":
            pid = request["project_id"]
            return {"project": {"id": pid, "name": "Stitch" if pid == A else "Other"},
                    "repositories": [{"id": REPO, "name": "api"}] if pid == A else [],
                    "applications": [], "tasks": [{"name": "tests", "service": "web", "mode": "exec"}]}
        if op == "transition_submit":
            return {"id": OPERATION, "action": request["action"],
                    "project_id": request["project_id"], "status": "queued"}
        if op == "transition_status":
            return {"id": request["id"], "action": "activate",
                    "project_id": A, "status": "succeeded"}
        if op in ("repository_check", "repository_update"):
            if request["project_id"] != A or request["repository_id"] != REPO:
                raise ValueError("Wrong repository")
            return {"branch": "main", "head": HEAD, "dirty": False,
                    "sync": {"status": "updated", "policy": "manual"}}
        if op in ("compose_service", "task"):
            return {"job_id": JOB}
        if op == "job":
            return {"id": request["id"], "project_id": request["project_id"],
                    "status": "running", "cancel_policy": "independent"}
        if op == "job_cancel":
            return {"id": request["id"], "project_id": request["project_id"],
                    "status": "cancelled"}
        raise AssertionError(f"Unexpected controller operation: {op}")


class DirectControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket = Path(self.tmp.name) / "control.sock"
        self.stub = ControllerStub()
        self.server = ControlServer(self.socket, self.stub)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = MCPReadClient(self.socket)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.tmp.cleanup()

    def test_read_and_transition_use_real_ipc_without_web(self):
        self.assertEqual(self.client.probe(), "connected")
        self.assertEqual(self.client.runtime()["active_project"]["id"], A)
        result = self.client.transition("activate", A, "f" * 32)
        self.assertEqual(result["project"]["id"], A)
        self.assertEqual(result["operation"]["id"], OPERATION)
        self.assertEqual(self.stub.calls[-1], {
            "op": "transition_submit", "action": "activate", "project_id": A,
            "request_key": "f" * 32,
        })
        done = self.client.transition_status(OPERATION)
        self.assertEqual(done["operation"]["status"], "succeeded")
        self.assertEqual(done["project"]["name"], "Stitch")
        stopped = self.client.transition("deactivate", None)
        self.assertEqual(stopped["scope"], "devstack")
        self.assertIsNone(stopped["operation"]["project_id"])

    def test_write_operations_require_identity_and_preconditions(self):
        checked = self.client.repository_check(A, REPO)
        self.assertEqual(checked["source"]["id"], REPO)
        updated = self.client.repository_update(A, REPO, HEAD)
        self.assertEqual(updated["previous_head"], HEAD)
        self.assertEqual(self.stub.calls[-1], {
            "op": "repository_update", "project_id": A,
            "repository_id": REPO, "expected_head": HEAD})
        action = self.client.service_action(A, "web", "restart")
        self.assertEqual(action["job_id"], JOB)
        task = self.client.run_task(A, "tests")
        self.assertEqual(task["job_id"], JOB)
        self.assertEqual(self.client.job_detail(A, JOB)["job"]["status"], "running")
        self.assertEqual(self.client.cancel_job(A, JOB)["job"]["status"], "cancelled")

    def test_invalid_or_foreign_resources_never_reach_mutating_controller_operation(self):
        cases = [
            (self.client.transition, ("activate", None)),
            (self.client.transition, ("reconcile", A)),
            (self.client.transition, ("switch", A, "invalid-key")),
            (self.client.repository_check, (B, REPO)),
            (self.client.repository_update, (A, REPO, "not-a-sha")),
            (self.client.repository_update, (B, REPO, HEAD)),
            (self.client.service_action, (A, "../secret", "restart")),
            (self.client.service_action, (A, "web", "destroy")),
            (self.client.run_task, (A, "../bash")),
            (self.client.run_task, (A, "unknown")),
            (self.client.cancel_job, (B, "not-a-uuid")),
        ]
        for method, args in cases:
            with self.subTest(method=method.__name__, args=args):
                before = len(self.stub.calls)
                with self.assertRaises((ValueError, MCPReadError)):
                    method(*args)
                self.assertTrue(all(call["op"] not in (
                    "transition_submit", "repository_update", "repository_check",
                    "compose_service", "task", "job_cancel",
                ) for call in self.stub.calls[before:]))

    def test_systemd_mcp_is_independent_of_web_and_not_in_docker(self):
        self.assertIn("SupplementaryGroups=devstackpublic devstackipc", PUBLIC_UNIT)
        self.assertIn("Requires=devstack-control.service", PUBLIC_UNIT)
        self.assertNotIn("Requires=devstack-web-v2.service", PUBLIC_UNIT)
        self.assertNotIn("SupplementaryGroups=docker", PUBLIC_UNIT)
        legacy = UNIT_FILES["devstack-mcp.service"]
        self.assertIn("SupplementaryGroups=devstackipc", legacy)
        self.assertNotIn("Requires=devstack-web-v2.service", legacy)


if __name__ == "__main__":
    unittest.main()
