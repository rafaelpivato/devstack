"""A single dedicated GitHub SSH identity for the Devstack service account.

Never accept or display a private key in the dashboard. Host keys are acquired
from GitHub's authenticated HTTPS API, not an unauthenticated ssh-keyscan.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import urllib.request

KEY_NAME = "id_ed25519"
GITHUB_META = "https://api.github.com/meta"
GITHUB_GREETING = re.compile(
    r"Hi ([A-Za-z0-9-]+)! You've successfully authenticated", re.I
)
KEY_TYPES = {"ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"}


class GitHubSSHError(RuntimeError):
    pass


def ssh_directory() -> Path:
    return Path.home() / ".config" / "devstack" / "ssh"


def key_path() -> Path:
    return ssh_directory() / KEY_NAME


def public_key() -> str | None:
    key = key_path()
    pub = key.with_name(key.name + ".pub")
    if key.is_symlink() or pub.is_symlink():
        raise GitHubSSHError("SSH identity files must not be symlinks")
    if not key.exists():
        return None
    if not pub.is_file():
        raise GitHubSSHError("SSH public key is missing; restore from backup")
    value = pub.read_text(encoding="utf-8").strip()
    if not value.startswith("ssh-ed25519 ") or "\n" in value:
        raise GitHubSSHError("The SSH public key has an unexpected format")
    return value


def generate_key() -> str:
    directory = ssh_directory()
    if directory.is_symlink():
        raise GitHubSSHError("Refusing to use an SSH directory symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    key = key_path()
    if key.exists() or key.with_name(key.name + ".pub").exists():
        raise GitHubSSHError("A Devstack SSH identity already exists; refusing replacement")
    with tempfile.TemporaryDirectory(prefix="key-", dir=directory) as tmp:
        staging = Path(tmp) / KEY_NAME
        try:
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                 "devstack-github", "-f", str(staging)],
                capture_output=True, check=True, text=True, timeout=20,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise GitHubSSHError("Could not generate an SSH key on the server") from exc
        staging.chmod(0o600)
        (staging.with_name(KEY_NAME + ".pub")).chmod(0o644)
        # The directory is private to the service user. Do not replace keys.
        if key.exists():
            raise GitHubSSHError("SSH key already exists")
        os.replace(staging, key)
        os.replace(staging.with_name(KEY_NAME + ".pub"),
                   key.with_name(KEY_NAME + ".pub"))
    result = public_key()
    assert result
    return result


def refresh_github_host_keys() -> None:
    """Trust GitHub's keys via authenticated HTTPS, never blind SSH TOFU."""
    req = urllib.request.Request(
        GITHUB_META,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "devstack/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.load(response)
    except (OSError, ValueError) as exc:
        raise GitHubSSHError("Cannot retrieve GitHub host keys over HTTPS") from exc
    raw_keys = payload.get("ssh_keys")
    if not isinstance(raw_keys, list):
        raise GitHubSSHError("GitHub host keys are not available")
    known = []
    for item in raw_keys:
        if not isinstance(item, str) or "\n" in item or "\r" in item:
            continue
        parts = item.split()
        if len(parts) == 2 and parts[0] in KEY_TYPES and len(parts[1]) >= 40:
            if re.fullmatch(r"[A-Za-z0-9+/=]+", parts[1]):
                known.append("github.com " + item)
    if not known:
        raise GitHubSSHError("GitHub returned no recognizable SSH host keys")
    directory = ssh_directory()
    if directory.is_symlink():
        raise GitHubSSHError("Refusing SSH directory symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    target = directory / "known_hosts"
    if target.is_symlink():
        raise GitHubSSHError("Refusing known_hosts symlink")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory,
        prefix=".known-hosts-", delete=False
    ) as staging:
        stage_path = Path(staging.name)
        staging.write("\n".join(known) + "\n")
    try:
        stage_path.chmod(0o600)
        os.replace(stage_path, target)
    finally:
        stage_path.unlink(missing_ok=True)


def ssh_command() -> str:
    if public_key() is None:
        raise GitHubSSHError("Generate your GitHub SSH key first")
    key = key_path()
    hosts = ssh_directory() / "known_hosts"
    if not hosts.is_file() or hosts.is_symlink():
        raise GitHubSSHError("GitHub host keys not initialized; test the connection first")
    parts = [
        "ssh", "-i", str(key), "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10", "-o",
        f"UserKnownHostsFile={hosts}",
    ]
    return " ".join(shlex.quote(part) for part in parts)


def git_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = ssh_command()
    return env


def test_connection() -> str:
    refresh_github_host_keys()
    command = shlex.split(ssh_command())
    try:
        result = subprocess.run(
            [*command, "-T", "git@github.com"],
            capture_output=True, text=True, check=False, timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubSSHError("GitHub SSH connection failed or timed out") from exc
    # GitHub deliberately returns exit code 1 on successful SSH auth.
    text = result.stderr + "\n" + result.stdout
    match = GITHUB_GREETING.search(text)
    if result.returncode == 1 and match:
        return match.group(1)
    if result.returncode != 1 or not match:
        raise GitHubSSHError(
            "GitHub did not accept this key yet. Add its public key to GitHub, "
            "check the required organization SSO authorization, and retry."
        )
    raise GitHubSSHError("GitHub SSH authentication could not be verified")
