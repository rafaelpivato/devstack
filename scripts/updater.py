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


def healthy(url: str, attempts: int = 90) -> bool:
    """Require the new web API, registry-backed controller and inactive runtime.

    An HTTP 200 from an old singleton web process cannot satisfy this gate.
    No DevStack-managed runtime may auto-start after a release activation.
    """
    if url != "http://127.0.0.1:8765/healthz":
        raise ValueError("Only the local DevStack v2 health endpoint is accepted")
    runtime_url = "http://127.0.0.1:8765/api/v2/runtime"
    for attempt in range(attempts):
        try:
            health_request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(health_request, timeout=2) as response:
                health = json.load(response)
            status_request = urllib.request.Request(
                runtime_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(status_request, timeout=2) as response:
                runtime = json.load(response)
            if (health == {"ok": True, "controller": "connected"}
                    and runtime.get("phase") == "inactive"
                    and runtime.get("project_id") is None
                    and runtime.get("generation") is None):
                return True
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if attempt != attempts - 1:
            time.sleep(1)
    return False


def cli_collision(root: Path, destination: Path = Path("/usr/local/bin/dev"),
                  release_cli: Path | None = None) -> bool:
    """Preflight a new versioned CLI without modifying any host commands."""
    selected = root / "current" / "venv" / "bin" / "dev"
    existing = shutil.which("dev")
    selected_executable = release_cli if release_cli is not None else selected
    if (existing and Path(existing) != destination
            and Path(existing).resolve() != selected_executable.resolve()):
        raise RuntimeError(f"Refusing to shadow existing 'dev' at {existing}")
    if not os.path.lexists(destination):
        return False
    if destination.is_symlink() and os.readlink(destination) == str(selected):
        return False
    raise RuntimeError(f"Refusing to overwrite unrelated 'dev' at {destination}")


def restart_services() -> None:
    """Web has no Docker access. Restart sole Docker owner before the web API."""
    execute("systemctl", "stop", "devstack-web-v2.service", timeout=45)
    execute("systemctl", "restart", "devstack-control.service", timeout=240)
    execute("systemctl", "start", "devstack-web-v2.service", timeout=45)
    execute("systemctl", "is-active", "--quiet", "devstack-control.service", timeout=10)
    execute("systemctl", "is-active", "--quiet", "devstack-web-v2.service", timeout=10)


def activate(
    tag: str, repo: str, root: Path, state: Path,
    health_url: str = "http://127.0.0.1:8765/healthz",
) -> str:
    tag = checked_tag(tag)
    if tuple(map(int, tag[1:].split("."))) < (0, 5, 2):
        raise ValueError("v0.5.1 or earlier cannot run on the clean-break v2 installer")
    repo = checked_repo(repo)
    root.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    current = root / "current"
    failed = state / ("failed-" + tag)
    if failed.exists():
        return f"Skipping previously failed release {tag}; operator action required to retry"
    old = current_release(current)
    target = root / "releases" / tag
    if old == target and (target / ".ready").exists():
        # No service restart is needed; an actively running project is not
        # a failed update. A health gate is required only before a change.
        return f"Already running {tag}"
    # An update must not disrupt an active project or unhealthy supervisor.
    if old is not None and not healthy(health_url, attempts=2):
        raise RuntimeError(
            "Deactivate all projects and restore v2 health before updating"
        )
    target = prepare_release(root, tag, repo)
    cli_available = (target / "venv" / "bin" / "dev").is_file()
    if cli_available:
        # Perform the collision check BEFORE activating the new release.
        cli_collision(root, release_cli=target / "venv" / "bin" / "dev")
    cli_link = Path("/usr/local/bin/dev")
    had_cli_link = os.path.lexists(cli_link)
    point_at(current, target)
    try:
        restart_services()
        if not healthy(health_url):
            raise RuntimeError(f"Controller+web reconciliation health check failed for {tag}")
        if cli_available:
            # The root updater runs the release-owned collision-safe setup.
            # It installs a symlink to /opt/devstack/current, never a copied CLI.
            execute(str(target / "venv" / "bin" / "python"), "-m",
                    "devstack.cli.install", "--root", str(root), timeout=25)
    except Exception as exc:
        # Do not leave the new command dangling after rollback to v0.5.2.
        if (cli_available and not had_cli_link and cli_link.is_symlink()
                and os.readlink(cli_link) ==
                str(root / "current" / "venv" / "bin" / "dev")):
            cli_link.unlink()
        # The updater remains outside the versioned application and uses only
        # the trusted prior v2 release. Never roll back to singleton v0.5.1.
        point_at(current, old)
        if old:
            try:
                restart_services()
            except Exception:
                pass
        else:
            subprocess.run(["systemctl", "stop", "devstack-web-v2.service"],
                           check=False, timeout=45)
            subprocess.run(["systemctl", "stop", "devstack-control.service"],
                           check=False, timeout=180)
        failed.write_text(type(exc).__name__ + "\n", encoding="utf-8")
        raise RuntimeError(
            f"Update to {tag} failed, restored previous release pointer; check logs"
        ) from exc
    try:
        execute("systemctl", "try-restart", "devstack-bootstrap.service", timeout=30)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"DevStack bootstrap reload warning: {type(exc).__name__}",
              file=sys.stderr, flush=True)
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
