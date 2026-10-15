"""Single-project runtime lifecycle, independent of Flask and the dashboard.

The controller is intentionally outside Docker. A persistent state record is
only an observation, never proof that Docker resources have stopped. Reconcile
on each controller boot; never silently resume a project.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

from .workspace import load_project

PHASES = {"inactive", "activating", "active", "deactivating", "error"}
TRANSITIONS = {"activate": "activating", "deactivate": "deactivating"}
CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
TOKEN = re.compile(r"[0-9a-f]{24}\Z")
MAX_RUN_SECONDS = 900


class RuntimeControlError(RuntimeError):
    """A runtime transition is invalid, blocked, or could not be verified."""


def _folder() -> Path:
    folder = Path.home() / ".config" / "devstack"
    if folder.is_symlink():
        raise RuntimeControlError("Runtime directory must not be a symlink")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    return folder


def _state_path() -> Path:
    path = _folder() / "runtime.json"
    if path.is_symlink():
        raise RuntimeControlError("Runtime state file must not be a symlink")
    return path


def status() -> dict:
    """Nonblocking read: the UI can observe a lengthy Docker operation."""
    path = _state_path()
    if not path.exists():
        return {"phase": "inactive", "project": None}
    if path.stat().st_size > 8192:
        raise RuntimeControlError("Runtime state is too large")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict)
                or record.get("phase") not in PHASES
                or (record.get("project") is not None and not isinstance(record.get("project"), str))):
            raise ValueError("Invalid runtime state")
        return record
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeControlError("Runtime state is invalid; refusing activation") from exc


def _write(record: dict) -> None:
    path = _state_path()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".runtime-", delete=False) as file:
        temp = Path(file.name)
        json.dump(record, file, sort_keys=True)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    try:
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _locked(*, blocking: bool = False, shared: bool = False):
    path = _folder() / ".runtime.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                        | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise RuntimeControlError("A runtime operation is already in progress") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def admit(name: str, *, shared: bool = False, blocking: bool = False):
    """Serialize the admission/launch boundary with runtime transitions.

    A worker must take this lock *immediately before* starting Docker work;
    queued work alone conveys no authorization. Nonblocking acquisition
    prevents deadlocks while shutdown is waiting for existing workers.
    """
    with _locked(shared=shared, blocking=blocking):
        current = status()
        if current.get("phase") != "active" or current.get("project") != name:
            raise RuntimeControlError("Project execution is allowed only while active")
        yield


def boot_id() -> str | None:
    """Linux boot identity, used to invalidate orphaned workers after reboot."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None


def is_active(name: str) -> bool:
    """Advisory poll for running workers, which must stop on deactivation."""
    try:
        current = status()
        return current.get("phase") == "active" and current.get("project") == name
    except (RuntimeControlError, OSError, ValueError):
        return False


def _stop_jobs(name: str) -> None:
    """Ask all managed background jobs to stop and verify they have settled.

    This runs while the controller holds the exclusive runtime lock. Workers
    poll the unguarded persisted phase; no worker may be newly admitted.
    """
    from .command_runner import cancel_all as cancel_commands
    from .docker_stacks import cancel_all as cancel_compose
    try:
        cancel_commands(name)
        cancel_compose(name)
    except (OSError, ValueError) as exc:
        raise RuntimeControlError("Cannot inspect active project jobs") from exc
    except Exception as exc:
        # Both job modules expose safe-to-display domain errors. Do not mark
        # INACTIVE if either reports a running process we could not stop.
        from .command_runner import CommandError
        from .docker_stacks import DockerError
        if isinstance(exc, (CommandError, DockerError)):
            raise RuntimeControlError("Project workers could not be stopped") from exc
        raise


def _project_name() -> str:
    project = load_project()
    if project is None:
        raise RuntimeControlError("Configure the single project first")
    return project.name


def _docker(args: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeControlError("Docker operation unavailable or timed out") from exc
    if result.returncode:
        raise RuntimeControlError("Docker operation failed")
    return result


def _owned_ids(name: str) -> list[str]:
    from .docker_stacks import compose_name
    ids: set[str] = set()
    for label in (f"com.docker.compose.project={compose_name(name)}",
                  f"devstack.project={compose_name(name)}"):
        result = _docker(["docker", "ps", "-a", "--filter",
                          "label=" + label, "--format", "{{.ID}}"], timeout=30)
        for value in result.stdout.splitlines():
            identifier = value.strip()
            if not CONTAINER_ID.fullmatch(identifier):
                raise RuntimeControlError("Unrecognized Docker container identifier")
            ids.add(identifier)
    return sorted(ids)


def _clean(name: str) -> None:
    """Remove only containers bearing the exact managed Compose project label.

    Compose down preserves named volumes. If the YAML has since disappeared
    or is invalid, the label-verified fallback still removes owned containers.
    """
    from .docker_stacks import compose_command, compose_path, project_root, DockerError
    try:
        if compose_path(name).is_file():
            try:
                _docker(compose_command(name) + ["down", "--remove-orphans"], timeout=240)
            except RuntimeControlError:
                # A missing or broken Compose service does not prevent cleanup
                # of containers whose ownership Docker labels establish.
                pass
    except (DockerError, OSError):
        pass
    ids = _owned_ids(name)
    if ids:
        _docker(["docker", "rm", "-f", *ids], timeout=90)
    if _owned_ids(name):
        raise RuntimeControlError("Project containers could not be removed")


def _validated_compose(name: str) -> list[str]:
    """Reject Docker-managed restart policies that bypass inactive state."""
    from .docker_stacks import compose_command, compose_path, project_root
    if not compose_path(name).is_file():
        raise RuntimeControlError("Save a Compose definition before activation")
    cmd = compose_command(name)
    data = _docker(cmd + ["config", "--format", "json"], timeout=30)
    try:
        config = json.loads(data.stdout)
        services = config["services"]
        if not isinstance(services, dict) or not services:
            raise ValueError("No services")
        for service in services.values():
            if not isinstance(service, dict):
                raise ValueError("Invalid service")
            if service.get("restart", "no") not in ("no", None):
                raise RuntimeControlError("Compose restart policies are not supported for managed projects")
            deploy = service.get("deploy") or {}
            policy = deploy.get("restart_policy") or {}
            if policy and policy.get("condition", "none") != "none":
                raise RuntimeControlError("Compose deploy restart policies are not supported")
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeControlError("Unable to validate Compose services") from exc
    return cmd


def request_transition(action: str) -> dict:
    """Persist intent before forking a worker; only one transition may queue."""
    if action not in TRANSITIONS:
        raise RuntimeControlError("Unsupported runtime transition")
    # A transition waits for already-admitted public webhooks to finish.
    # No request can cross the ACTIVE -> DEACTIVATING boundary mid-forward.
    with _locked(blocking=True):
        name = _project_name()
        current = status()
        phase = current["phase"]
        if phase in ("activating", "deactivating"):
            raise RuntimeControlError("Runtime transition already queued")
        if current.get("project") not in (None, name):
            raise RuntimeControlError("Stored project identity does not match configuration")
        if action == "activate":
            if phase != "inactive":
                raise RuntimeControlError("Deactivate or recover the runtime before activation")
            _validated_compose(name)
        elif phase == "inactive":
            raise RuntimeControlError("Runtime is already inactive")
        token = secrets.token_hex(12)
        record = {"phase": TRANSITIONS[action], "project": name,
                  "token": token, "updated_at": time.time()}
        _write(record)
        try:
            subprocess.Popen([sys.executable, "-m", "devstack.runtime", "--worker",
                              action, name, token],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True)
        except OSError as exc:
            _write({"phase": "error", "project": name,
                    "message": "Could not launch runtime worker",
                    "updated_at": time.time()})
            raise RuntimeControlError("Could not launch runtime worker") from exc
        return {"phase": record["phase"], "project": name}


def perform(action: str, name: str, token: str) -> int:
    """Run the queued action under the same exclusive lock as reconciliation."""
    if action not in TRANSITIONS or not TOKEN.fullmatch(token):
        return 2
    with _locked(blocking=True):
        record = status()
        if (record.get("phase") != TRANSITIONS[action] or
                record.get("project") != name or record.get("token") != token or
                _project_name() != name):
            return 2
        try:
            if action == "activate":
                command = _validated_compose(name)
                _stop_jobs(name)
                _clean(name)
                _docker(command + ["up", "--detach", "--build", "--wait",
                                   "--wait-timeout", "180"], timeout=MAX_RUN_SECONDS)
                phase = "active"
            else:
                _stop_jobs(name)
                _clean(name)
                phase = "inactive"
            _write({"phase": phase, "project": name, "updated_at": time.time()})
            return 0
        except (RuntimeControlError, OSError, ValueError):
            # Never announce an active runtime after an uncertain Docker error.
            if action == "activate":
                try:
                    _clean(name)
                except (RuntimeControlError, OSError, ValueError):
                    pass
            _write({"phase": "error", "project": name,
                    "message": "Runtime transition failed; inspect Docker before retrying",
                    "updated_at": time.time()})
            return 1


def reconcile() -> bool:
    """Fail closed at every controller startup, including after a host reboot.

    A stale queued worker cannot resume: reconciliation invalidates its token.
    Report ERROR (not INACTIVE) if Docker cannot prove cleanup succeeded.
    """
    with _locked(blocking=True):
        current = status()
        configured = load_project()
        names = set()
        if configured is not None:
            names.add(configured.name)
        if current.get("project"):
            names.add(current["project"])
        try:
            for name in sorted(names):
                _stop_jobs(name)
                _clean(name)
            _write({"phase": "inactive", "project": configured.name if configured else None,
                    "updated_at": time.time()})
            return True
        except (RuntimeControlError, OSError, ValueError):
            _write({"phase": "error", "project": configured.name if configured else None,
                    "message": "Startup cleanup could not be verified",
                    "updated_at": time.time()})
            return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--worker", nargs=3, metavar=("ACTION", "PROJECT", "TOKEN"))
    args = parser.parse_args()
    if args.reconcile:
        # Keep dashboard available to show recoverable cleanup errors.
        try:
            if not reconcile():
                print("Runtime reconciliation incomplete; operator recovery required",
                      file=sys.stderr, flush=True)
        except (RuntimeControlError, OSError, ValueError) as exc:
            # State corruption is fail-closed; no activation happens on boot.
            try:
                _write({"phase": "error", "project": None,
                        "message": "Runtime reconciliation unavailable",
                        "updated_at": time.time()})
            except (RuntimeControlError, OSError):
                pass
            print(f"Runtime reconciliation unavailable: {exc}",
                  file=sys.stderr, flush=True)
        return 0
    if args.worker:
        return perform(*args.worker)
    parser.error("Specify --reconcile or --worker")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
