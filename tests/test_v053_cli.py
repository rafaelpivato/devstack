"""Native SSH CLI foundation: flags, output and failure contract."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from devstack.cli import main as cli
from devstack.cli.install import CLIInstallError, authorize_operator, install_command
from devstack.ipc.client import (
    ControllerIncompatible, ControllerRejected, ControllerUnavailable,
)
from scripts import updater


class CLICommandTests(unittest.TestCase):
    def invoke(self, *argv, status=None, version=None, error=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(cli, "ensure_compatible",
                         return_value=version or {"major": 1, "minor": 0}) as compatible,
            patch.object(cli, "send", side_effect=error,
                         return_value=status or {"phase": "inactive", "project_id": None}) as sender,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = cli.main(list(argv))
        return result, stdout.getvalue(), stderr.getvalue(), compatible, sender

    def test_help_never_connects(self):
        for argv in (["--help"], ["status", "--help"], ["doctor", "--help"]):
            with self.subTest(argv=argv), patch.object(cli, "send") as client:
                with redirect_stdout(io.StringIO()) as buf:
                    with self.assertRaises(SystemExit) as done:
                        cli.main(argv)
                self.assertEqual(done.exception.code, 0)
                self.assertIn("usage:", buf.getvalue())
                client.assert_not_called()

    def test_status_and_doctor_share_unprivileged_socket(self):
        rc, out, err, compatibility, sender = self.invoke("status")
        self.assertEqual((rc, out, err), (0, "Runtime: inactive\n", ""))
        self.assertEqual(sender.call_args.args[1], "status")
        self.assertEqual(compatibility.call_args.args[0],
                         Path("/run/devstack-control/control.sock"))
        rc, out, err, _, _ = self.invoke("doctor")
        self.assertEqual(rc, 0)
        self.assertIn("IPC: compatible (1.0)", out)
        self.assertIn("Runtime: inactive", out)
        self.assertEqual(err, "")

    def test_json_flag_accepts_both_positions(self):
        status = {"phase": "active", "project_id": "6f" * 18, "message": ""}
        for args in (("--json", "status"), ("status", "--json")):
            with self.subTest(args=args):
                rc, out, err, _, _ = self.invoke(*args, status=status)
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(out), status)
                self.assertEqual(err, "")
                self.assertEqual(len(out.splitlines()), 1)
        rc, out, err, _, _ = self.invoke("--json", "doctor", status=status)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["protocol"]["major"], 1)
        self.assertEqual(err, "")

    def test_mobile_output_40_60_80_columns(self):
        import shutil
        from uuid import uuid4
        project_id = str(uuid4())
        status = {"phase": "active", "project_id": project_id,
                  "message": "An error detail that needs to fit a small terminal width"}
        for columns in (40, 60, 80):
            with self.subTest(columns=columns):
                with patch.object(cli.shutil, "get_terminal_size",
                                  return_value=os.terminal_size((columns, 30))):
                    output = cli._render_status(status)
                self.assertTrue(all(len(line) <= columns for line in output.splitlines()))
                self.assertIn(project_id, output)
        self.assertIs(shutil, cli.shutil)

    def test_custom_socket_is_forwarded_and_does_not_enable_shell(self):
        rc, _, _, compat, sender = self.invoke("status", "--socket", "/tmp/test.sock")
        self.assertEqual(rc, 0)
        self.assertEqual(compat.call_args.args[0], Path("/tmp/test.sock"))
        self.assertEqual(sender.call_args.args, (Path("/tmp/test.sock"), "status"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as done:
            cli.main(["shell", "docker", "ps"])
        self.assertEqual(done.exception.code, 2)

    def test_incompatible_controller_has_stable_exit(self):
        rc, out, err, _, sender = self.invoke(
            "status", error=None)
        self.assertEqual(rc, 0)
        with patch.object(cli, "ensure_compatible",
                          side_effect=ControllerIncompatible("unsafe detail")):
            with redirect_stdout(io.StringIO()) as so, redirect_stderr(io.StringIO()) as se:
                self.assertEqual(cli.main(["status"]), cli.EX_CONFIG)
            self.assertEqual(so.getvalue(), "")
            self.assertNotIn("unsafe detail", se.getvalue())

    def test_unauthorized_ssh_user_has_stable_exit(self):
        error = ControllerUnavailable("Controller is unavailable")
        error.__cause__ = PermissionError("private host path")
        with patch.object(cli, "ensure_compatible", side_effect=error):
            with redirect_stdout(io.StringIO()) as so, redirect_stderr(io.StringIO()) as se:
                self.assertEqual(cli.main(["status"]), cli.EX_NOPERM)
            self.assertEqual(so.getvalue(), "")
            self.assertIn("access denied", se.getvalue())
            self.assertNotIn("private host path", se.getvalue())

    def test_unavailable_and_rejected_errors_are_sanitized(self):
        for error, expected in (
            (ControllerUnavailable("/var/lib/devstack/private"), cli.EX_UNAVAILABLE),
            (ControllerRejected("ProjectConflict"), cli.EX_TEMPFAIL),
            (ControllerRejected("ProtocolError"), cli.EX_DATAERR),
        ):
            with self.subTest(error=type(error)):
                with patch.object(cli, "ensure_compatible", side_effect=error):
                    with redirect_stdout(io.StringIO()) as so, redirect_stderr(io.StringIO()) as se:
                        self.assertEqual(cli.main(["doctor"]), expected)
                    self.assertEqual(so.getvalue(), "")
                    self.assertNotIn("/var/lib/devstack/private", se.getvalue())


class CLIInstallTests(unittest.TestCase):
    def test_collision_protection_idempotency_and_pointer_rollback(self):
        with tempfile.TemporaryDirectory() as rootdir:
            root = Path(rootdir) / "install"
            first = root / "releases" / "v0.5.3"
            second = root / "releases" / "v0.5.4"
            for release in (first, second):
                executable = release / "venv" / "bin" / "dev"
                executable.parent.mkdir(parents=True)
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            updater.point_at(root / "current", first)
            destination = Path(rootdir) / "dev"
            self.assertTrue(install_command(root, destination))
            self.assertFalse(install_command(root, destination))
            self.assertEqual(destination.resolve(), first / "venv/bin/dev")
            updater.point_at(root / "current", second)
            self.assertEqual(destination.resolve(), second / "venv/bin/dev")
            updater.point_at(root / "current", first)
            self.assertEqual(destination.resolve(), first / "venv/bin/dev")

            collision = Path(rootdir) / "other"
            collision.write_text("not DevStack\n")
            with self.assertRaises(CLIInstallError):
                install_command(root, collision)
            self.assertEqual(collision.read_text(), "not DevStack\n")
            destination.unlink()
            destination.symlink_to(collision)
            with self.assertRaises(CLIInstallError):
                install_command(root, destination)
            self.assertEqual(destination.resolve(), collision)

    def test_uninstalled_or_nonexecuting_entry_point_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            root.mkdir()
            with self.assertRaises(CLIInstallError):
                install_command(root, Path(directory) / "dev")

    def test_updater_preflight_blocks_existing_dev_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            destination = Path(directory) / "dev"
            self.assertFalse(updater.cli_collision(root, destination))
            destination.write_text("other vendor\n")
            with self.assertRaisesRegex(RuntimeError, "overwrite unrelated"):
                updater.cli_collision(root, destination)
            self.assertEqual(destination.read_text(), "other vendor\n")
            destination.unlink()
            destination.symlink_to(root / "current/venv/bin/dev")
            self.assertFalse(updater.cli_collision(root, destination))

    def test_enrolling_operator_requires_real_interactive_user(self):
        import pwd
        from types import SimpleNamespace
        with patch.object(pwd, "getpwnam", side_effect=KeyError):
            with self.assertRaises(CLIInstallError):
                authorize_operator("unknown")
        for uid, shell in ((999, "/bin/bash"), (1000, "/usr/sbin/nologin")):
            with patch.object(pwd, "getpwnam",
                              return_value=SimpleNamespace(pw_uid=uid, pw_shell=shell)):
                with self.assertRaises(CLIInstallError):
                    authorize_operator("operator")
        with patch.object(pwd, "getpwnam",
                          return_value=SimpleNamespace(pw_uid=1000, pw_shell="/bin/bash")):
            with patch("devstack.cli.install.subprocess.run") as called:
                authorize_operator("operator")
            self.assertEqual(called.call_args.args[0],
                             ["usermod", "-aG", "devstackipc", "operator"])

    def test_cli_has_no_privileged_dependencies(self):
        from pathlib import Path
        source = Path(cli.__file__).read_text()
        for forbidden in ("import docker", "import sqlite3", "import subprocess",
                          "import pwd", "from devstack.control"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
