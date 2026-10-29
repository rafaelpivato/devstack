"""v0.7.8: MCP project/repository management delegates to shared controller.

No Web HTTP or extra authorization layer; command shape is exact controller IPC.
"""
from pathlib import Path
import tempfile
import threading
import unittest

from devstack.control.daemon import ControlServer
from devstack.mcp_reads import MCPReadClient, MCPReadError

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
REPO = "33333333-3333-4333-8333-333333333333"
OP = "44444444-4444-4444-8444-444444444444"
JOB = "55555555-5555-4555-8555-555555555555"
REV = "a" * 64
KEY = "c" * 32


class Stub:
    def __init__(self):
        self.calls = []
        self.projects = {"stitch": {"id": A, "slug": "stitch", "name": "Stitch"},
                         "other": {"id": B, "slug": "other", "name": "Other"}}

    def handle(self, request):
        self.calls.append(request)
        op = request["op"]
        if op == "projects":
            return {"projects": list(self.projects.values())}
        if op == "project_detail":
            pid = request["project_id"]
            if pid not in (A, B):
                raise ValueError("not found")
            return {"project": next(x for x in self.projects.values() if x["id"] == pid),
                    "repositories": [{"id": REPO, "name": "api"}] if pid == A else [],
                    "applications": [], "tasks": []}
        if "project_id" in request and request["project_id"] not in (A, B):
            raise ValueError("foreign project")
        if op.startswith("repository_") and op not in ("repository_add_submit",
                                                       "repository_add_status",
                                                       "repository_bulk_submit",
                                                       "repository_bulk_status") and request.get("repository_id") != REPO:
            raise ValueError("foreign repository")
        if op == "repository_sync_state":
            return {"policy": "manual", "status": "idle"}
        if op == "repository_sync_policy":
            return {"policy": request["policy"], "status": "idle"}
        if op in ("repository_reactions", "repository_reaction_policy"):
            return {"services": [{"name": "web", "action": request.get("action", "none")}],
                    "events": []}
        if op in ("repository_bulk_submit", "repository_bulk_status",
                  "repository_add_submit", "repository_add_status"):
            return {"project_id": request["project_id"], "id": OP, "status": "queued"}
        if op == "branch_preview":
            return {"project_id": request["project_id"],
                    "repository_id": request["repository_id"],
                    "requested_branch": request["branch"], "revision": REV,
                    "ready": True, "entries": []}
        if op == "branch_apply":
            return {"project_id": request["project_id"],
                    "requested_branch": request["branch"], "complete": True, "results": []}
        if op == "project_create":
            self.projects[request["slug"]] = {"id": OP, "slug": request["slug"],
                                               "name": request["name"]}
            return {"id": OP, "name": request["name"]}
        if op in ("import_submit", "import_status"):
            return {"id": OP, "status": "queued", "project_id": None}
        if op == "project_rename":
            self.projects["stitch"]["name"] = request["name"]
            return self.projects["stitch"]
        if op == "compose_read":
            return {"content": "services:\n  web:\n    image: nginx\n", "revision": REV}
        if op == "compose_save":
            return {"revision": REV, "saved": True}
        if op == "settings_review":
            return {"revision": REV, "applications": [], "tasks": []}
        if op == "settings_replace_checked":
            return {"applications": request["applications"], "tasks": request["tasks"]}
        if op == "source_review":
            return {"revision": REV, "changes": []}
        if op == "source_apply":
            return {"applied": request["selected"]}
        if op == "webhooks":
            return {"webhooks": [{"route": "github", "service": "web", "port": 8000,
                                  "destination": "/hook"}]}
        if op == "webhook_upsert":
            return {k: request[k] for k in ("route", "service", "port", "destination")}
        if op == "webhook_remove":
            return {"removed": True}
        if op in ("compose", "adhoc"):
            return {"job_id": JOB}
        if op == "private_ports":
            return {"project_id": request["project_id"], "ports": []}
        raise ValueError("unknown operation")


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "controller.sock"
        self.stub = Stub()
        self.server = ControlServer(self.path, self.stub)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.mcp = MCPReadClient(self.path)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.temp.cleanup()

    def test_repository_sync_policies_and_reactions(self):
        sync = self.mcp.repository_sync(A, REPO)
        self.assertEqual(sync["project"]["id"], A)
        self.assertEqual(sync["sync"]["policy"], "manual")
        updated = self.mcp.repository_sync_policy(A, REPO, "update")
        self.assertEqual(updated["sync"]["policy"], "update")
        self.assertIn({"op": "repository_sync_policy", "project_id": A,
                       "repository_id": REPO, "policy": "update"}, self.stub.calls)
        reactions = self.mcp.repository_reaction_policy(A, REPO, "web", "redeploy")
        self.assertEqual(reactions["services"][0]["action"], "redeploy")
        self.assertEqual(self.mcp.repository_reactions(A, REPO)["project"]["id"], A)

    def test_repository_bulk_add_and_status(self):
        op = self.mcp.repository_bulk_update(A, KEY)
        self.assertEqual(op["operation"]["id"], OP)
        self.assertEqual(self.mcp.repository_bulk_status(A, OP)["operation"]["status"], "queued")
        added = self.mcp.repository_add(A, "https://github.com/org/repo", "api", "main", KEY)
        self.assertEqual(added["operation"]["project_id"], A)
        self.assertEqual(self.mcp.repository_add_status(A, OP)["source"]["id"], OP)
        self.assertIn({"op": "repository_add_submit", "project_id": A,
                       "url": "https://github.com/org/repo", "name": "api",
                       "branch": "main", "request_key": KEY}, self.stub.calls)

    def test_review_branch_and_apply_revision(self):
        preview = self.mcp.branch_preview(A, "feature/hello", REPO)
        self.assertTrue(preview["preview"]["ready"])
        self.assertEqual(preview["preview"]["revision"], REV)
        applied = self.mcp.branch_apply(A, "feature/hello", REV, REPO)
        self.assertTrue(applied["result"]["complete"])
        self.assertIn({"op": "branch_apply", "project_id": A,
                       "branch": "feature/hello", "repository_id": REPO,
                       "revision": REV}, self.stub.calls)

    def test_create_import_rename(self):
        created = self.mcp.create_project("new", "New Project")
        self.assertEqual(created["project"]["id"], OP)
        imported = self.mcp.import_project("https://github.com/org/repo", "new2", "Another", KEY)
        self.assertEqual(imported["operation"]["id"], OP)
        self.assertEqual(self.mcp.import_status(OP)["operation"]["status"], "queued")
        renamed = self.mcp.rename_project(A, "Stitch App")
        self.assertEqual(renamed["project"]["name"], "Stitch App")
        self.assertIn({"op": "project_rename", "project_id": A,
                       "name": "Stitch App"}, self.stub.calls)

    def test_compose_and_settings_revision_contracts(self):
        config = self.mcp.compose_config(A)
        self.assertEqual(config["config"]["revision"], REV)
        saved = self.mcp.compose_save(A, config["config"]["content"], REV)
        self.assertTrue(saved["result"]["saved"])
        settings = self.mcp.project_settings(A)
        self.assertEqual(settings["settings"]["revision"], REV)
        result = self.mcp.settings_save(A, REV, [], [])
        self.assertEqual(result["result"]["tasks"], [])
        self.assertIn({"op": "settings_replace_checked", "project_id": A,
                       "revision": REV, "applications": [], "tasks": []},
                      self.stub.calls)

    def test_source_webhooks_and_jobs(self):
        self.assertEqual(self.mcp.source_review(A)["review"]["revision"], REV)
        self.assertEqual(self.mcp.source_apply(A, REV, ["compose.yaml"], [])["result"],
                         {"applied": ["compose.yaml"]})
        self.assertEqual(self.mcp.webhooks(A)["webhooks"][0]["route"], "github")
        webhook = self.mcp.webhook_upsert(A, "github", "web", 8000, "/hook")
        self.assertEqual(webhook["webhook"]["route"], "github")
        self.assertTrue(self.mcp.webhook_remove(A, "github")["removed"])
        self.assertEqual(self.mcp.compose_action(A, "build")["job_id"], JOB)
        result = self.mcp.adhoc(A, "run", "web", "", "none", ["python", "--version"])
        self.assertEqual(result["job_id"], JOB)
        self.assertEqual(self.mcp.private_ports(A)["project"]["id"], A)

    def test_invalid_requests_fail_before_controller_mutation(self):
        cases = [
            (self.mcp.repository_sync_policy, (A, REPO, "force")),
            (self.mcp.repository_reaction_policy, (A, REPO, "web", "reset")),
            (self.mcp.repository_sync, (B, REPO)),
            (self.mcp.repository_bulk_status, (A, "bad")),
            (self.mcp.repository_add, (A, "", "api")),
            (self.mcp.branch_apply, (A, "main", "wrong", REPO)),
            (self.mcp.branch_preview, (B, "main", REPO)),
            (self.mcp.compose_save, (A, "services: {}", "wrong")),
            (self.mcp.settings_save, (A, "wrong", [], [])),
            (self.mcp.source_apply, (A, "wrong", [], [])),
            (self.mcp.webhook_upsert, (A, "", "web", 8000, "/hook")),
            (self.mcp.compose_action, (A, "down")),
            (self.mcp.adhoc, (A, "host", "web", "", "none", ["sh"])),
        ]
        mutations = {"repository_sync_policy", "repository_reaction_policy",
                     "repository_bulk_submit", "repository_add_submit", "branch_apply",
                     "compose_save", "settings_replace_checked", "source_apply",
                     "webhook_upsert", "compose", "adhoc"}
        for fn, args in cases:
            with self.subTest(operation=fn.__name__):
                before = len(self.stub.calls)
                with self.assertRaises((MCPReadError, ValueError)):
                    fn(*args)
                self.assertFalse(any(call["op"] in mutations
                                     for call in self.stub.calls[before:]))


if __name__ == "__main__":
    unittest.main()
