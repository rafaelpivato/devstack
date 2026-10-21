"""I7: GitHub/repository workflows use existing controller IPC exclusively."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from devstack.cli import main as cli
from devstack.cli import git_commands as git
from devstack.control.daemon import ControlServer, Controller
from devstack.ipc.client import ControllerRejected, ControllerUnavailable
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database

PROJECT = str(uuid4())
REPO = str(uuid4())
OP = str(uuid4())
KEY = "b" * 32
HEAD = "c" * 40
REVISION = "d" * 64


class GitCLIContractTests(unittest.TestCase):
    def run_cli(self, *args, response=None, side_effect=None):
        out, err = io.StringIO(), io.StringIO()
        with (patch.object(cli, "ensure_compatible",
                           return_value={"major": 1, "minor": 1}) as compatible,
              patch.object(cli, "send", side_effect=side_effect,
                           return_value=response) as sender,
              redirect_stdout(out), redirect_stderr(err)):
            status = cli.main(list(args))
        return status, out.getvalue(), err.getvalue(), compatible, sender

    def test_import_and_retry_key_do_not_depend_on_ssh_session(self):
        receipt = {"id": OP, "slug": "my-project", "status": "queued",
                   "project_id": None, "error_code": ""}
        argv = ("project", "import", "https://github.com/example/demo",
                "--slug", "my-project", "--name", "My Project")
        rc, out, err, _, sender = self.run_cli(*argv, "--json",
                                               "--request-key", KEY,
                                               response=receipt)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), receipt)
        self.assertIn(KEY, err)
        self.assertEqual(sender.call_args.args[1], "import_submit")
        self.assertEqual(sender.call_args.kwargs["request_key"], KEY)
        self.assertEqual(sender.call_args.kwargs["url"], argv[2])
        self.assertEqual(sender.call_args.kwargs["slug"], "my-project")

        rc, out, err, _, sender = self.run_cli(
            "project", "import-status", OP, response=receipt)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sender.call_args.args[1], "import_status")
        self.assertEqual(sender.call_args.kwargs["id"], OP)

    def test_repo_list_status_and_known_head_fast_forward(self):
        rc, out, err, _, sender = self.run_cli(
            "repo", "list", "--project", PROJECT,
            response={"repositories": [{"id": REPO, "name": "backend"}]})
        self.assertEqual(rc, 0, err)
        self.assertIn(REPO, out)
        self.assertEqual(sender.call_args.args[1], "project_detail")
        self.assertEqual(sender.call_args.kwargs["project_id"], PROJECT)

        status = {"id": REPO, "name": "backend", "head": HEAD, "branch": "main",
                  "upstream": "origin/main", "dirty": False, "ahead": 0, "behind": 1}
        for action in (
            ("repo", "status", REPO, "--project", PROJECT),
            ("repo", "update", REPO, "--project", PROJECT,
             "--expected-head", HEAD),
        ):
            rc, out, err, _, sender = self.run_cli(*action, response=status)
            self.assertEqual(rc, 0, err)
            self.assertIn(HEAD, out)
            self.assertEqual(sender.call_args.args[1],
                             "repository_update" if "update" in action else "repository_status")
            if "update" in action:
                self.assertEqual(sender.call_args.kwargs["expected_head"], HEAD)

    def test_repo_add_and_bulk_require_project_and_idempotency(self):
        receipt = {"id": OP, "status": "queued", "project_id": PROJECT,
                   "repo_id": None, "name": "backend", "error_code": ""}
        rc, out, err, _, sender = self.run_cli(
            "repo", "add", "git@github.com:example/backend.git",
            "--name", "backend", "--branch", "main", "--project", PROJECT,
            "--request-key", KEY, response=receipt)
        self.assertEqual(rc, 0, err)
        self.assertIn(OP, out)
        self.assertIn(KEY, err)
        self.assertEqual(sender.call_args.args[1], "repository_add_submit")
        self.assertEqual(sender.call_args.kwargs["branch"], "main")
        self.assertEqual(sender.call_args.kwargs["project_id"], PROJECT)

        rc, out, err, _, sender = self.run_cli(
            "repo", "update-all", "--project", PROJECT,
            "--request-key", KEY, "--json",
            response={"id": OP, "status": "queued", "items": []})
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["id"], OP)
        self.assertEqual(sender.call_args.args[1], "repository_bulk_submit")

        for action, operation in (("add-status", "repository_add_status"),
                                  ("update-status", "repository_bulk_status")):
            rc, _, err, _, sender = self.run_cli(
                "repo", action, OP, "--project", PROJECT, response=receipt)
            self.assertEqual(rc, 0, err)
            self.assertEqual(sender.call_args.args[1], operation)
            self.assertEqual(sender.call_args.kwargs["id"], OP)

    def test_branch_preview_apply_revision_is_explicit_and_scoped(self):
        preview = {
            "project_id": PROJECT, "requested_branch": "feature/a",
            "repository_id": REPO, "ready": True, "revision": REVISION,
            "entries": [{"repository_id": REPO, "name": "backend",
                         "action": "switch", "target_branch": "feature/a",
                         "fallback": False}],
        }
        rc, out, err, _, sender = self.run_cli(
            "repo", "branch-preview", "feature/a", "--project", PROJECT,
            "--repository", REPO, response=preview)
        self.assertEqual(rc, 0, err)
        self.assertIn(REVISION, out)
        self.assertEqual(sender.call_args.args[1], "branch_preview")
        self.assertEqual(sender.call_args.kwargs["repository_id"], REPO)

        rc, out, err, _, sender = self.run_cli(
            "repo", "branch-apply", "feature/a", "--project", PROJECT,
            "--repository", REPO, "--revision", REVISION,
            response={"branch": "feature/a", "results": []})
        self.assertEqual(rc, 0, err)
        self.assertEqual(sender.call_args.args[1], "branch_apply")
        self.assertEqual(sender.call_args.kwargs["revision"], REVISION)
        self.assertEqual(sender.call_args.kwargs["branch"], "feature/a")

    def test_wait_polls_durable_receipt_after_disconnect_and_errors(self):
        replies = [
            {"id": OP, "status": "queued", "slug": "alpha"},
            {"id": OP, "status": "running", "slug": "alpha"},
            {"id": OP, "status": "succeeded", "slug": "alpha",
             "project_id": PROJECT},
        ]
        with patch.object(git.time, "sleep") as sleep:
            rc, out, err, _, sender = self.run_cli(
                "project", "import", "https://github.com/example/demo",
                "--slug", "alpha", "--name", "Alpha", "--request-key", KEY,
                "--wait", response=None, side_effect=replies)
        self.assertEqual(rc, 0, err)
        self.assertIn("State: succeeded", out)
        self.assertEqual(sender.call_count, 3)
        self.assertEqual(sender.call_args.args[1], "import_status")
        self.assertEqual(sleep.call_count, 2)

        failing = [
            {"id": OP, "status": "queued"},
            {"id": OP, "status": "failed", "error_code": "ImportFailed"},
        ]
        with patch.object(git.time, "sleep"):
            rc, out, err, _, _ = self.run_cli(
                "project", "import", "https://github.com/example/demo",
                "--slug", "alpha", "--name", "Alpha", "--wait",
                side_effect=failing)
        self.assertEqual(rc, cli.EX_TEMPFAIL)
        self.assertEqual(out, "")
        self.assertIn("ended as failed", err)

    def test_invalid_arguments_fail_before_controller_contact(self):
        cases = (
            ("project", "import", "a b", "--slug", "good", "--name", "Good"),
            ("project", "import", "https://github.com/o/r", "--slug", "../oops",
             "--name", "A"),
            ("project", "import", "https://github.com/o/r", "--slug", "ok",
             "--name", " Name "),
            ("project", "import-status", "not-uuid"),
            ("repo", "list"),
            ("repo", "status", "not-uuid", "--project", PROJECT),
            ("repo", "update", REPO, "--project", PROJECT),
            ("repo", "update", REPO, "--project", PROJECT,
             "--expected-head", "HEAD"),
            ("repo", "branch-apply", "main", "--project", PROJECT),
            ("repo", "branch-apply", "main", "--project", PROJECT,
             "--revision", "123"),
            ("repo", "branch-preview", "../bad", "--project", PROJECT),
            ("repo", "add", "https://github.com/o/r", "--name", "../bad",
             "--project", PROJECT),
            ("repo", "update-all", "--project", PROJECT,
             "--request-key", "invalid"),
        )
        for args in cases:
            with self.subTest(args=args), patch.object(cli, "ensure_compatible") as network:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    cli.main(list(args))
                self.assertEqual(raised.exception.code, 2)
                network.assert_not_called()

    def test_bad_controller_rejections_remain_sanitized(self):
        rc, out, err, _, _ = self.run_cli(
            "repo", "update", REPO, "--project", PROJECT,
            "--expected-head", HEAD, side_effect=ControllerRejected("RepositoryGitError"))
        self.assertEqual(rc, cli.EX_TEMPFAIL)
        self.assertEqual(out, "")
        self.assertIn("RepositoryGitError", err)
        self.assertNotIn("private", err)

    def test_mobile_widths_and_no_direct_git(self):
        payload = {"project_id": PROJECT, "requested_branch": "release",
                   "ready": True, "revision": REVISION,
                   "entries": [{"name": "X" * 120, "target_branch": "release",
                                "action": "switch", "fallback": False}]}
        for cols in (40, 60, 80):
            with patch.object(cli.shutil, "get_terminal_size",
                              return_value=os.terminal_size((cols, 20))):
                rc, out, err, _, _ = self.run_cli(
                    "repo", "branch-preview", "release", "--project", PROJECT,
                    response=payload)
            self.assertEqual(rc, 0, err)
            self.assertTrue(all(len(row) <= cols for row in out.splitlines()),
                            (cols, out))
        for module in (cli, git):
            content = Path(module.__file__).read_text()
            for forbidden in ("import subprocess", "import docker", "import sqlite3",
                              "from devstack.control"):
                self.assertNotIn(forbidden, content)


class GitCLIControllerIPC(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.controller = Controller(
            Database(root / "state" / "db.sqlite3"), WorkspaceStore(root / "projects"),
        )
        self.project = self.controller.provisioner.create_empty("demo", "Demo")
        self.socket = root / "control.sock"
        server = ControlServer(self.socket, self.controller)
        self.addCleanup(server.server_close)
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()
        self.addCleanup(runner.join, 3)
        self.addCleanup(server.shutdown)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = cli.main(["--socket", str(self.socket), *args])
        return status, out.getvalue(), err.getvalue()

    def test_empty_repo_list_and_reject_unsafe_branch_apply(self):
        rc, out, err = self.run_cli("repo", "list", "--project", self.project.id)
        self.assertEqual(rc, 0, err)
        self.assertIn("No repositories", out)
        rc, out, err = self.run_cli(
            "repo", "branch-apply", "main", "--project", self.project.id,
            "--revision", REVISION)
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")

    def test_explicit_controller_deny_invalid_github_host(self):
        rc, out, err = self.run_cli(
            "project", "import", "https://example.com/o/r", "--slug",
            "not-github", "--name", "Not GitHub", "--request-key", KEY)
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("rejected", err)


if __name__ == "__main__":
    unittest.main()
