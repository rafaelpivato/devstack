"""GitHub SSH provisioning stays on the service account; only public keys appear in HTTP."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from devstack import github_ssh as ssh


class GitHubSSHTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch("devstack.github_ssh.ssh_directory",
                        return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ed25519_generation_once(self):
        key = ssh.generate_key()
        self.assertTrue(key.startswith("ssh-ed25519 "))
        self.assertEqual(ssh.public_key(), key)
        self.assertEqual(ssh.key_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.tmp.name).stat().st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(ssh.GitHubSSHError, "already exists"):
            ssh.generate_key()

    def test_trusted_keys_from_https(self):
        payload = {"ssh_keys": ["ssh-ed25519 " + "Q" * 60,
                                "ssh-ed25519 x\ninjected", "malformed"]}
        stream = MagicMock()
        stream.__enter__.return_value = __import__("io").BytesIO(
            json.dumps(payload).encode()
        )
        with patch("devstack.github_ssh.urllib.request.urlopen",
                   return_value=stream) as request:
            ssh.refresh_github_host_keys()
        url = request.call_args.args[0]
        self.assertEqual(url.full_url, "https://api.github.com/meta")
        lines = (Path(self.tmp.name) / "known_hosts").read_text().splitlines()
        self.assertEqual(lines, ["github.com ssh-ed25519 " + "Q" * 60])

    def test_git_env_rejects_absent_identity(self):
        with self.assertRaises(ssh.GitHubSSHError):
            ssh.git_environment()

    def test_never_uses_ambient_agent(self):
        ssh.generate_key()
        (Path(self.tmp.name) / "known_hosts").write_text(
            "github.com ssh-ed25519 " + "Q" * 60
        )
        command = ssh.git_environment()["GIT_SSH_COMMAND"]
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("IdentityAgent=none", command)
        self.assertIn("known_hosts", command)

    def test_test_connection_accepts_github_exit_one(self):
        ssh.generate_key()
        with patch.object(ssh, "refresh_github_host_keys") as update, \
             patch.object(ssh, "ssh_command", return_value="ssh") as command, \
             patch("devstack.github_ssh.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1,
                stdout="", stderr="Hi some-user! You've successfully authenticated, "
                "but GitHub does not provide shell access.")
            self.assertEqual(ssh.test_connection(), "some-user")
            update.assert_called_once_with()
            command.assert_called_once_with()

    def test_test_connection_does_not_show_cli_errors(self):
        with patch.object(ssh, "refresh_github_host_keys"), \
             patch.object(ssh, "ssh_command", return_value="ssh"), \
             patch("devstack.github_ssh.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=255,
                stdout="", stderr="potentially sensitive diagnostic")
            with self.assertRaises(ssh.GitHubSSHError) as error:
                ssh.test_connection()
            self.assertNotIn("sensitive", str(error.exception))


if __name__ == "__main__":
    unittest.main()
