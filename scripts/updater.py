#!/usr/bin/env python3
"""Independent, conservative release updater. Only requires Python stdlib + git.

Installed outside /opt/devstack/current so a broken web app can roll back.
Normal upgrades happen only after a GitHub Release is published.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TAG_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
USER_AGENT = "devstack-updater/0.1"


def checked_tag(tag: str) -> str:
    if not TAG_RE.fullmatch(tag):
        raise ValueError("Only stable vMAJOR.MINOR.PATCH releases are accepted")
    return tag


def checked_repo(repo: str) -> str:
    if not REPO_RE.fullmatch(repo) or ".." in repo:
        raise ValueError("Invalid GitHub repository name")
    return repo


def latest_tag(repo: str) -> str:
    repo = checked_repo(repo)
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        release = json.load(response)
    return checked_tag(release["tag_name"])


def execute(*args: str, timeout: int = 180) -> None:
    subprocess.run(list(args), check=True, timeout=timeout)


def prepare_release(root: Path, tag: str, repo: str) -> Path:
    """Install an immutable-by-convention tagged checkout in its own venv."""
    checked_tag(tag)
    checked_repo(repo)
    releases = root / "releases"
    release_dir = releases / tag
    ready = release_dir / ".ready"
    if ready.exists():
        return release_dir
    releases.mkdir(parents=True, exist_ok=True)
    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir()
    try:
        source = release_dir / "source"
        execute("git", "clone", "--depth", "1", "--single-branch",
                "--branch", tag, f"https://github.com/{repo}.git",
                str(source), timeout=180)
        execute(sys.executable, "-m", "venv", str(release_dir / "venv"), timeout=90)
        # A release tag must correspond to the checked-out Python package
        # version, not just a Git ref with an arbitrary name.
        import tomllib
        metadata = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
        if "v" + metadata["project"]["version"] != tag:
            raise RuntimeError("Release tag disagrees with pyproject.toml version")
        execute(str(release_dir / "venv/bin/python"), "-m", "pip", "install",
                "--disable-pip-version-check", "--no-input", "--no-cache-dir",
                str(source), timeout=240)
        ready.write_text(tag + "\n", encoding="utf-8")
    except BaseException:
        shutil.rmtree(release_dir, ignore_errors=True)
        raise
    return release_dir


def current_release(current: Path) -> Path | None:
    if not current.is_symlink():
        if current.exists():
            raise RuntimeError("Refusing to replace a non-symlink current path")
        return None
    target = current.resolve(strict=True)
    return target


def point_at(current: Path, target: Path | None) -> None:
    """Swap the release pointer atomically, or remove it on initial failure."""
    if target is None:
        current.unlink(missing_ok=True)
        return
    temp = current.with_name(".current-" + secrets.token_hex(6))
    os.symlink(target, temp)
    try:
        os.replace(temp, current)
    finally:
        temp.unlink(missing_ok=True)


def healthy(url: str, attempts: int = 30) -> bool:
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status == 200 and json.load(response).get("status") == "ok":
                    return True
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if attempt != attempts - 1:
            time.sleep(1)
    return False


def activate(
    tag: str, repo: str, root: Path, state: Path,
    service: str = "devstack.service",
    health_url: str = "http://127.0.0.1:8765/healthz",
) -> str:
    tag = checked_tag(tag)
    repo = checked_repo(repo)
    if service != "devstack.service":
        raise ValueError("Service must be devstack.service")
    root.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    current = root / "current"
    failed = state / ("failed-" + tag)
    if failed.exists():
        return f"Skipping previously failed release {tag}; operator action required to retry"
    old = current_release(current)
    target = root / "releases" / tag
    if old == target and (target / ".ready").exists():
        return f"Already running {tag}"
    target = prepare_release(root, tag, repo)
    point_at(current, target)
    try:
        execute("systemctl", "restart", service, timeout=30)
        if not healthy(health_url):
            raise RuntimeError(f"Health check failed for {tag}")
    except Exception as exc:
        # Updater runs outside the versioned application. Rollback is independent.
        point_at(current, old)
        subprocess.run(
            ["systemctl", "restart" if old else "stop", service],
            check=False, timeout=30,
        )
        failed.write_text(str(exc)[:1000] + "\n", encoding="utf-8")
        raise RuntimeError(f"Update to {tag} failed, restored previous release: {exc}") from exc
    # The public first-use portal is independent from the Flask app. If it is
    # running, reload it from the same reviewed release as the main service.
    # The unit points at /opt/devstack/current/source/scripts/bootstrap.py.
    try:
        execute("systemctl", "try-restart", "devstack-bootstrap.service", timeout=30)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"Devstack bootstrap reload warning: {exc}", file=sys.stderr, flush=True)
    return f"Activated {tag}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Install a specific stable release instead of latest")
    args = parser.parse_args()
    root = Path(os.environ.get("DEVSTACK_ROOT", "/opt/devstack"))
    state = Path(os.environ.get("DEVSTACK_STATE", "/var/lib/devstack"))
    repo = checked_repo(os.environ.get("DEVSTACK_REPO", "rafaelpivato/devstack"))
    state.mkdir(parents=True, exist_ok=True)
    with (state / "update.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            tag = checked_tag(args.tag) if args.tag else latest_tag(repo)
            print(activate(tag, repo, root, state), flush=True)
            return 0
        except Exception as exc:
            print(f"Devstack update error: {exc}", file=sys.stderr, flush=True)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
