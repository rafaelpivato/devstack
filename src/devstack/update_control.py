"""Safe, unprivileged trigger and read-only status for the root-owned updater.

The application can only create a marker in a private RuntimeDirectory.
A root-owned systemd.path unit consumes that marker and starts the existing
independent update service; no shell, sudo, privileged API, or user payload.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import urllib.request

REQUEST_DIRECTORY = Path("/run/devstack-update-request")
LATEST_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_cache_lock = threading.Lock()
_cache_value: tuple[float, str] | None = None


class UpdateControlError(RuntimeError):
    """A safe-to-display update control error."""


def installed_version() -> str:
    return "v" + importlib.metadata.version("devstack")


def latest_version() -> str:
    """Short-lived cache avoids hammering GitHub as browsers poll."""
    global _cache_value
    with _cache_lock:
        if _cache_value is not None and time.monotonic() - _cache_value[0] < 60:
            return _cache_value[1]
        repo = os.environ.get("DEVSTACK_RELEASE_REPO", "rafaelpivato/devstack")
        if not REPO_RE.fullmatch(repo) or ".." in repo:
            raise UpdateControlError("Invalid configured release repository")
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "devstack-update-control/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                tag = json.load(response)["tag_name"]
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise UpdateControlError("GitHub release information unavailable") from exc
        if not isinstance(tag, str) or not LATEST_RE.fullmatch(tag):
            raise UpdateControlError("GitHub did not return a stable release")
        _cache_value = (time.monotonic(), tag)
        return tag


def request_directory() -> Path:
    return Path(os.environ.get("DEVSTACK_UPDATE_REQUEST_DIR", str(REQUEST_DIRECTORY)))


def check_request_directory() -> Path:
    folder = request_directory()
    try:
        metadata = folder.lstat()
    except FileNotFoundError as exc:
        raise UpdateControlError(
            "Manual update trigger not installed. Run the one-time installer refresh."
        ) from exc
    if (not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise UpdateControlError("Update request directory has unexpected permissions")
    return folder


def update_state() -> dict[str, str]:
    """Read systemd's public unit properties; never read privileged logs."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "devstack-update.service", "--no-pager",
             "--property=ActiveState", "--property=Result",
             "--property=ExecMainStatus"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines()
                      if "=" in line)
    except (OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired, ValueError):
        fields = {}
    state = fields.get("ActiveState", "unknown")
    result = fields.get("Result", "unknown")
    code = fields.get("ExecMainStatus", "")
    if state == "activating":
        phase = "updating"
    elif state == "failed" or (result not in ("success", "unknown") and result):
        phase = "failed"
    else:
        phase = "idle"
    try:
        folder = check_request_directory()
        if (folder / "trigger").exists() and phase != "updating":
            phase = "queued"
    except UpdateControlError:
        pass
    return {"phase": phase, "unit_state": state, "last_result": result,
            "last_exit_code": code}


def request_update() -> str:
    folder = check_request_directory()
    # The watcher only reacts to existence. Contents are never interpreted.
    path = folder / "trigger"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return "Update request already queued"
    except OSError as exc:
        raise UpdateControlError("Unable to queue an update on this server") from exc
    os.close(fd)
    return "Update requested. The independent updater will check GitHub now."
