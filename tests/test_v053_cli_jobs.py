"""I6: native CLI job lifecycle, isolated cancellation and bounded log streams."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from devstack.cli import main as cli
from devstack.control.daemon import ControlServer, Controller
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database


def eventually(read, condition, *, timeout=7):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        item = read()
        if condition(item):
            return item
        time.sleep(.025)
    raise AssertionError("Timed out waiting for controller-owned job")


class TestBackend:
    def __init__(self):
        self.running = []
        self.stopped = []
        self.commands = []

    def owned(self):
        return list(self.running)

    def validate(self, identifier):
        return None

    def start(self, identifier, generation):
        self.running.append({
            "id": "a" * 64, "project": identifier,
            "generation": generation, "state": "running",
        })

    def verify(self, identifier, generation):
        if not any(x["project"] == identifier and x["generation"] == generation
                   for x in self.running):
            raise RuntimeError("Not running")

    def stop(self, identifier):
        self.running = [x for x in self.running if x["project"] != identifier]

    def task_command(self, identifier, spec, generation, job_id):
        self.commands.append((identifier, spec, generation, job_id))
        # Fake backend is the only place that constructs host argv in this test.
        # The CLI's request is merely a typed service/image + argv specification.
        duration = 2.0 if spec["argv"][0] == "long-task" else 0.10
        return [sys.executable, "-u", "-c",
                "import time;print('job-'+__import__('sys').argv[1],flush=True);"
                "time.sleep(float(__import__('sys').argv[2]));print('finished',flush=True)",
                job_id, str(duration)]

    def stop_job(self, identifier, generation, job_id):
        self.stopped.append((identifier, generation, job_id))


class JobCLIAcceptance(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.backend = TestBackend()
        self.controller = Controller(
            Database(root / "db" / "state.sqlite3"),
            WorkspaceStore(root / "projects"), self.backend,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "Alpha")
        self.b = self.controller.provisioner.create_empty("beta", "Beta")
        self.controller.coordinator.activate(self.a.id)
        self.socket = root / "control.sock"
        server = ControlServer(self.socket, self.controller)
        self.addCleanup(server.server_close)
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()
        self.addCleanup(runner.join, 3)
        self.addCleanup(server.shutdown)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = cli.main(["--socket", str(self.socket), *argv])
        return result, out.getvalue(), err.getvalue()

    def job(self, identifier):
        return self.controller.jobs.get(identifier)

    def submit(self, target="long-task", project=None, mode="run"):
        argv = ["job", mode, "web" if mode == "run" else "python:3.12-alpine",
                "--project", project or self.a.id]
        if mode == "image":
            argv += ["--network", "project"]
        argv += ["--", target, "-V"]
        rc, out, err = self.run_cli(*argv)
        self.assertEqual((rc, err), (0, ""))
        return out.removeprefix("Job: ").strip()

    def test_adhoc_compose_and_image_are_controller_owned_jobs(self):
        first = self.submit("long-task")
        second = self.submit("fast-task", mode="image")
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.backend.commands), 2)
        compose = self.backend.commands[0][1]
        standalone = self.backend.commands[1][1]
        self.assertEqual((compose["service"], compose["image"], compose["network"]),
                         ("web", "", "none"))
        self.assertEqual((standalone["service"], standalone["image"],
                          standalone["network"]), ("", "python:3.12-alpine", "project"))
        self.assertEqual(compose["argv"], ["long-task", "-V"])
        self.assertEqual(standalone["argv"], ["fast-task", "-V"])
        eventually(lambda: self.job(second), lambda j: j["status"] == "succeeded")
        # CLI session already exited, but the unrelated long-running job lives.
        self.assertIn(self.job(first)["status"], {"queued", "running", "succeeded"})
        rc, out, err = self.run_cli("job", "list", "--project", self.a.id, "--json")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual({j["id"] for j in json.loads(out)["jobs"]}, {first, second})
        rc, out, err = self.run_cli("job", "show", second,
                                    "--project", self.a.id)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("State: succeeded", out)
        self.assertIn("Exit code: 0", out)
        eventually(lambda: self.job(first), lambda j: j["status"] == "succeeded")

    def test_follow_is_incremental_json_ndjson_and_disconnection_is_inert(self):
        identifier = self.submit("long-task")
        eventually(lambda: self.controller.jobs.logs(self.a.id, identifier)["text"],
                   lambda txt: "job-" in txt)
        rc, out, err = self.run_cli(
            "job", "logs", identifier, "--project", self.a.id, "--json")
        self.assertEqual((rc, err), (0, ""))
        first = json.loads(out)
        self.assertIn("job-", first["text"])
        self.assertGreater(first["next_offset"], 0)
        rc, out, err = self.run_cli(
            "job", "logs", identifier, "--project", self.a.id,
            "--offset", str(first["next_offset"]))
        self.assertEqual((rc, err), (0, ""))
        self.assertNotIn("job-", out)

        # Ctrl-C interrupts the log follower, never the worker.
        with (patch.object(cli, "JOB_POLL_SECONDS", 0),
              patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt)):
            rc, out, err = self.run_cli(
                "job", "logs", identifier, "--project", self.a.id, "--follow")
        self.assertEqual(rc, 130)
        self.assertIn("interrupted", err)
        self.assertIn(self.job(identifier)["status"], {"queued", "running"})
        with patch.object(cli, "JOB_POLL_SECONDS", .01):
            rc, lines, err = self.run_cli(
                "job", "logs", identifier, "--project", self.a.id,
                "--follow", "--json")
        self.assertEqual((rc, err), (0, ""))
        chunks = [json.loads(row) for row in lines.splitlines()]
        self.assertTrue(chunks[-1]["complete"])
        self.assertIn("finished", "".join(p["text"] for p in chunks))
        self.assertTrue(all(
            earlier["next_offset"] <= later["next_offset"]
            for earlier, later in zip(chunks, chunks[1:])
        ))

    def test_independent_cancel_targets_one_job_and_keeps_project_active(self):
        first = self.submit("long-task")
        second = self.submit("long-task")
        eventually(lambda: self.job(first), lambda job: job["status"] == "running")
        eventually(lambda: self.job(second), lambda job: job["status"] == "running")
        rc, out, err = self.run_cli("job", "cancel", first, "--project", self.a.id)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("State: cancelled", out)
        self.assertEqual(self.job(first)["status"], "cancelled")
        self.assertIn(self.job(second)["status"], {"running", "succeeded"})
        self.assertEqual(len(self.backend.stopped), 1)
        self.assertEqual(self.backend.stopped[0][2], first)
        self.assertEqual(self.controller.coordinator.status()["phase"], "active")
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.a.id)
        eventually(lambda: self.job(second), lambda j: j["status"] == "succeeded")

    def test_wrong_project_access_denied_for_history_detail_logs_cancel(self):
        identifier = self.submit("long-task")
        for action in ("show", "logs", "cancel"):
            with self.subTest(action=action):
                rc, out, err = self.run_cli("job", action, identifier,
                                             "--project", self.b.id)
                self.assertEqual(rc, cli.EX_TEMPFAIL)
                self.assertEqual(out, "")
                self.assertIn("JobError", err)
        rc, out, err = self.run_cli("job", "list", "--project", self.b.id)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("No jobs", out)
        self.assertNotIn(identifier, out)
        eventually(lambda: self.job(identifier), lambda j: j["status"] == "succeeded")

    def test_invalid_syntax_fails_before_ipc(self):
        cases = (
            ("job", "run", "web", "--project", self.a.id, "python", "-V"),
            ("job", "run", "web", "--project", "alpha", "--", "python"),
            ("job", "run", "../secret", "--project", self.a.id, "--", "python"),
            ("job", "run", "web", "--project", self.a.id, "--"),
            ("job", "image", "bad name", "--project", self.a.id, "--", "python"),
            ("job", "logs", "not-a-uuid", "--project", self.a.id),
            ("job", "logs", str(uuid4()), "--project", self.a.id,
             "--offset", "-1"),
            ("job", "logs", str(uuid4()), "--project", self.a.id,
             "--offset", "2097153"),
            ("job", "cancel", str(uuid4())),
            ("job", "run", "web", "--project", self.a.id, "--", ""),
        )
        for argv in cases:
            with self.subTest(argv=argv), patch.object(cli, "ensure_compatible") as connect:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    cli.main(list(argv))
                self.assertEqual(raised.exception.code, 2)
                connect.assert_not_called()

    def test_bounded_rows_and_no_local_docker_engine(self):
        # Narrow mobile output wraps UUIDs without corrupting their identity.
        identifier = self.submit("fast-task")
        for columns in (40, 60, 80):
            with (patch.object(cli.shutil, "get_terminal_size",
                               return_value=os.terminal_size((columns, 24)))):
                rc, out, err = self.run_cli("job", "show", identifier,
                                            "--project", self.a.id)
            self.assertEqual((rc, err), (0, ""))
            self.assertIn(identifier, out)
            self.assertTrue(all(len(x) <= columns for x in out.splitlines()))
        content = Path(cli.__file__).read_text()
        for forbidden in ("import docker", "import subprocess", "import sqlite3",
                          "from devstack.control", "import pwd"):
            self.assertNotIn(forbidden, content)
        eventually(lambda: self.job(identifier), lambda j: j["status"] == "succeeded")


if __name__ == "__main__":
    unittest.main()
