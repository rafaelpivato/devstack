"""Container command runner safety, persistence and process lifecycle tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from devstack.command_runner import (
    CommandError, Task, _argv, _parse_tasks, load_tasks, save_task,
    delete_task, tasks_path, start_command, start_task, cancel_command,
    get_job, recent_jobs, job_output, work, services,
)
from devstack.core import Project


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / "workspaces" / "sample"
        (self.root / ".devstack").mkdir(parents=True)
        (self.root / ".devstack" / "compose.yaml").write_text(
            "services:\n  web:\n    image: python:3\n"
        )
        configs = patch("devstack.docker_stacks.load_projects",
                        return_value={"sample": Project("sample", (), ())})
        configs.start()
        self.addCleanup(configs.stop)
        home = patch("devstack.docker_stacks.Path.home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)

    def test_saved_tasks_round_trip_without_repo_modifications(self):
        (self.root / "repo").mkdir()
        save_task("sample", "tests", "web", "exec", 'python -m unittest "test app"')
        self.assertEqual(load_tasks("sample"), (
            Task("tests", "web", "exec", ("python", "-m", "unittest", "test app")),
        ))
        self.assertEqual(tasks_path("sample").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list((self.root / "repo").iterdir()), [])
        save_task("sample", "tests", "web", "run", ["python", "--version"])
        self.assertEqual(load_tasks("sample")[0].mode, "run")
        delete_task("sample", "tests")
        self.assertEqual(load_tasks("sample"), ())

    def test_parse_tasks_rejects_untrusted_fields(self):
        for definition in (
            '[[tasks]]\nname="bad"\nservice="web"\nmode="exec"\ncommand=["echo","ok"]\nunknown=1',
            '[[tasks]]\nname="foo"\nservice="web"\nmode="shell"\ncommand=["echo"]',
            '[[tasks]]\nname="foo"\nservice="web"\nmode="exec"\ncommand="echo hello"',
        ):
            with self.assertRaises(CommandError):
                _parse_tasks(definition)

    def test_arguments_use_shlex_but_no_host_shell(self):
        self.assertEqual(_argv('python -c "print(123)"'),
                         ("python", "-c", "print(123)"))
        self.assertEqual(_argv("echo ok | cat"), ("echo", "ok", "|", "cat"))
        for value in ("", 'echo "unterminated', "x" * 4200, ["echo", "\x00"]):
            with self.assertRaises(CommandError):
                _argv(value)

    @patch("devstack.command_runner.subprocess.run")
    def test_services_are_from_compose(self, run):
        from subprocess import CompletedProcess
        run.return_value = CompletedProcess([], 0, "web\nredis\npostgres\n", "")
        self.assertEqual(services("sample"), ("web", "redis", "postgres"))
        self.assertIn("config", run.call_args.args[0])
        self.assertNotIn("up", run.call_args.args[0])

    @patch("devstack.command_runner.subprocess.Popen")
    @patch("devstack.command_runner.services", return_value=("web", "redis"))
    def test_queue_persists_and_prevents_overlapping_work(self, names, popen):
        token = start_command("sample", "web", "exec", "python -m unittest")
        job = get_job("sample", token)
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["argv"], ["python", "-m", "unittest"])
        self.assertEqual(len(recent_jobs("sample")), 1)
        self.assertEqual(job_output("sample", token), "")
        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        with self.assertRaisesRegex(CommandError, "Another command"):
            start_command("sample", "redis", "run", "redis-cli ping")
        with self.assertRaisesRegex(CommandError, "not defined"):
            start_command("sample", "fake", "exec", "echo bad")
        cancel_command("sample", token)
        self.assertTrue((self.root / ".devstack" / "commands" / (token + ".cancel")).exists())

    @patch("devstack.command_runner.subprocess.Popen")
    @patch("devstack.command_runner.services", return_value=("web",))
    def test_saved_task_runs_and_invalid_ids_are_rejected(self, names, popen):
        save_task("sample", "tests", "web", "run", "python --version")
        identifier = start_task("sample", "tests")
        self.assertEqual(get_job("sample", identifier)["mode"], "run")
        for bad in ("../x", "x" * 20, "a" * 19, "a" * 21):
            with self.assertRaises(CommandError):
                get_job("sample", bad)

    @patch("devstack.command_runner.subprocess.Popen")
    @patch("devstack.command_runner.services", return_value=("web",))
    def test_worker_exec_command_uses_t_without_shell(self, names, popen):
        identifier = start_command("sample", "web", "exec", 'python -c "print(7)"')
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0
        popen.return_value = process
        self.assertEqual(work("sample", identifier), 0)
        args = popen.call_args.args[0]
        self.assertEqual(args[-6:], ["exec", "-T", "web", "python", "-c", "print(7)"])
        self.assertEqual(get_job("sample", identifier)["state"], "success")

    @patch("devstack.command_runner.subprocess.Popen")
    @patch("devstack.command_runner.services", return_value=("web",))
    def test_run_command_names_ephemeral_container(self, names, popen):
        identifier = start_command("sample", "web", "run", "python --version")
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0
        popen.return_value = process
        self.assertEqual(work("sample", identifier), 0)
        args = popen.call_args.args[0]
        self.assertEqual(args[-9:], [
            "run", "--rm", "--no-deps", "-T", "--name",
            "devstack-task-" + identifier, "web", "python", "--version"
        ])

    @patch("devstack.command_runner._terminate")
    @patch("devstack.command_runner.time.sleep")
    @patch("devstack.command_runner.subprocess.Popen")
    @patch("devstack.command_runner.services", return_value=("web",))
    def test_cancellation_marker_causes_termination(self, names, popen, sleep, terminate):
        identifier = start_command("sample", "web", "run", "python -m timeit")
        proc = Mock()
        proc.poll.return_value = None
        popen.return_value = proc
        marker = self.root / ".devstack" / "commands" / (identifier + ".cancel")
        sleep.side_effect = lambda _: marker.touch()
        self.assertEqual(work("sample", identifier), 1)
        terminate.assert_called_once_with(proc, run_container="devstack-task-" + identifier)
        self.assertEqual(get_job("sample", identifier)["state"], "cancelled")
        self.assertIsNone(get_job("sample", identifier)["exit_code"])
        with self.assertRaises(CommandError):
            cancel_command("sample", identifier)

    def test_refuse_symlink_task_or_history_paths(self):
        outside = self.home / "outside.toml"
        outside.write_text("secret")
        tasks_path("sample").symlink_to(outside)
        with self.assertRaises(CommandError):
            load_tasks("sample")
        self.assertEqual(outside.read_text(), "secret")


if __name__ == "__main__":
    unittest.main()
