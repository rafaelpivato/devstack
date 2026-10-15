"""Per-project Docker Compose files and background lifecycle jobs.

Managed Compose YAML lives at ~/workspaces/<project>/.devstack/compose.yaml,
never inside cloned Git repositories. The existing systemd Devstack controller
remains independent of Docker.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tomllib
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

from .core import load_projects

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
ACTIONS = {
    "build": ("build",),
    "restart": ("restart",),  # Starting/stopping belongs to runtime.activate/deactivate.
}
MAX_COMPOSE_BYTES = 128 * 1024
MAX_JOB_SECONDS = 900
DEFAULT_COMPOSE = """# This file belongs to Devstack, not to a Git repository.
# Relative paths are resolved from this .devstack directory.
name: devstack-project

services:
  app:
    image: nginx:alpine
    expose:
      - "80"
"""


class DockerError(RuntimeError):
    """Safe-to-display Docker operations error."""


def _checked_project(name: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name) or ".." in name:
        raise DockerError("Invalid project name")
    if name not in load_projects():
        raise DockerError("Project not found")
    return name


def project_root(name: str) -> Path:
    name = _checked_project(name)
    workspaces = Path.home() / "workspaces"
    root = workspaces / name
    # ProjectSetup creates these paths. Never follow a project's symlink.
    if workspaces.is_symlink() or root.is_symlink() or not root.is_dir():
        raise DockerError("Project workspace does not exist or is a symlink")
    return root


def managed_dir(name: str) -> Path:
    target = project_root(name) / ".devstack"
    if target.is_symlink():
        raise DockerError("Managed directory must not be a symlink")
    return target


def compose_path(name: str) -> Path:
    path = managed_dir(name) / "compose.yaml"
    if path.is_symlink():
        raise DockerError("Managed Compose file must not be a symlink")
    return path


def compose_name(name: str) -> str:
    # Safe and stable Docker Compose project name, even with dots/uppercase.
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")[:35] or "project"
    digest = hashlib.sha256(name.encode()).hexdigest()[:10]
    return f"devstack-{slug}-{digest}"


def imported_compose_arguments(project_name: str, file: Path, base: Path) -> list[str]:
    """Keep relative build contexts and mounts anchored at the Git template."""
    return ["docker", "compose", "-p", project_name,
            "--project-directory", str(base), "-f", str(file)]


def _imported_base(name: str) -> Path | None:
    """Resolve the original .devstack directory from local import metadata."""
    meta = managed_dir(name) / "import.toml"
    if meta.is_symlink():
        raise DockerError("Import metadata must not be a symlink")
    if not meta.exists():
        return None
    if meta.stat().st_size > 4096:
        raise DockerError("Import metadata is too large")
    try:
        data = tomllib.loads(meta.read_text(encoding="utf-8"))
        if set(data) != {"origin"} or set(data["origin"]) != {"repository", "subdirectory"}:
            raise ValueError("Invalid import metadata")
        repo = data["origin"]["repository"]
        subdir = data["origin"]["subdirectory"]
        if not isinstance(repo, str) or not NAME.fullmatch(repo) or ".." in repo:
            raise ValueError("Invalid imported repository")
        if not isinstance(subdir, str):
            raise ValueError("Invalid imported directory")
        parts = subdir.split("/") if subdir else []
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("Invalid imported directory")
        root = project_root(name)
        source = root.joinpath(repo, *parts, ".devstack")
        if not source.is_dir() or not source.resolve().is_relative_to(root.resolve()):
            raise ValueError("Original imported template is unavailable")
        return source
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise DockerError("Invalid or missing imported source template") from exc


def compose_command(name: str, file: Path | None = None) -> list[str]:
    file = file or compose_path(name)
    source = _imported_base(name)
    if source is None:
        return ["docker", "compose", "-p", compose_name(name), "-f", str(file)]
    return imported_compose_arguments(compose_name(name), file, source)


def docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True,
            timeout=5, check=False,
        )
        if result.returncode != 0:
            return False
        daemon = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return daemon.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def read_compose(name: str) -> str:
    path = compose_path(name)
    if not path.exists():
        return DEFAULT_COMPOSE.replace("devstack-project", compose_name(name))
    if path.stat().st_size > MAX_COMPOSE_BYTES:
        raise DockerError("Compose file exceeds the editor size limit")
    return path.read_text(encoding="utf-8")


def save_compose(name: str, content: str) -> None:
    path = compose_path(name)
    if not content.strip() or len(content.encode("utf-8")) > MAX_COMPOSE_BYTES:
        raise DockerError("Compose YAML must be nonempty and under 128 KiB")
    folder = managed_dir(name)
    folder.mkdir(mode=0o700, parents=False, exist_ok=True)
    folder.chmod(0o700)
    lock_path = folder / ".compose.lock"
    if lock_path.is_symlink():
        raise DockerError("Compose lock must not be a symlink")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=folder,
            prefix=".compose-", suffix=".yaml", delete=False
        ) as file:
            staging = Path(file.name)
            file.write(content)
        try:
            staging.chmod(0o600)
            # Compose performs YAML validation, interpolation and service checks.
            # Neither check nor save executes container commands.
            try:
                proc = subprocess.run(
                    compose_command(name, staging) + ["config", "--quiet"],
                    cwd=project_root(name),
                    capture_output=True, text=True, timeout=20, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DockerError("Docker Compose is unavailable or validation timed out") from exc
            if proc.returncode:
                raise DockerError("Invalid Compose configuration: " +
                                  (proc.stderr or proc.stdout).strip()[:600])
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)
            fcntl.flock(lock, fcntl.LOCK_UN)


def _run_compose(name: str, args: tuple[str, ...], timeout: int = 15) -> str:
    file = compose_path(name)
    if not file.is_file():
        raise DockerError("Save a managed Compose file first")
    try:
        proc = subprocess.run(
            compose_command(name) + list(args),
            cwd=project_root(name), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerError("Docker Compose unavailable or timed out") from exc
    if proc.returncode:
        raise DockerError("Docker Compose error: " +
                          (proc.stderr or proc.stdout).strip()[:800])
    return proc.stdout


def stack_status(name: str) -> str:
    return _run_compose(name, ("ps",), timeout=15) or "No containers running."


def stack_logs(name: str) -> str:
    return _run_compose(name, ("logs", "--no-color", "--tail", "100"), timeout=20)[-24000:]


def _jobs_dir(name: str) -> Path:
    folder = managed_dir(name) / "jobs"
    if folder.is_symlink():
        raise DockerError("Job directory must not be a symlink")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    return folder


def _read_job(folder: Path) -> dict | None:
    file = folder / "latest.json"
    if not file.exists():
        return None
    try:
        info = json.loads(file.read_text(encoding="utf-8"))
        return info if isinstance(info, dict) else None
    except (OSError, ValueError):
        return None


def _write_job(folder: Path, data: dict) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=folder, prefix=".job-",
        delete=False
    ) as file:
        temp = Path(file.name)
        json.dump(data, file)
    try:
        temp.chmod(0o600)
        os.replace(temp, folder / "latest.json")
    finally:
        temp.unlink(missing_ok=True)


def current_job(name: str) -> dict | None:
    folder = _jobs_dir(name)
    info = _read_job(folder)
    if not info:
        return None
    # A terminated/restarted web process must not leave an infinite lock.
    if info.get("state") in ("queued", "running"):
        if time.time() - float(info.get("started", 0)) > MAX_JOB_SECONDS + 30:
            return {**info, "state": "stale"}
    return info


def job_log(name: str) -> str:
    folder = _jobs_dir(name)
    log = folder / "latest.log"
    if not log.is_file() or log.is_symlink():
        return ""
    with log.open("rb") as file:
        file.seek(0, os.SEEK_END)
        file.seek(max(0, file.tell() - 25000))
        return file.read().decode("utf-8", "replace")


def queue_action(name: str, action: str) -> None:
    if action not in ACTIONS:
        raise DockerError("Unsupported Docker action")
    if not compose_path(name).is_file():
        raise DockerError("Save your Compose file before running containers")
    folder = _jobs_dir(name)
    with (folder / ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        info = current_job(name)
        if info and info.get("state") in ("queued", "running"):
            raise DockerError("A Docker operation is already running")
        job_id = secrets.token_hex(8)
        info = {"id": job_id, "action": action, "state": "queued",
                "started": time.time()}
        from . import runtime
        try:
            with runtime.admit(name):
                (folder / "latest.cancel").unlink(missing_ok=True)
                (folder / "latest.log").write_text("", encoding="utf-8")
                _write_job(folder, info)
                # Queueing does not authorize execution: worker rechecks at launch.
                subprocess.Popen(
                    [sys.executable, "-m", "devstack.docker_stacks",
                     "--job", name, action, job_id],
                    cwd=project_root(name),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                    close_fds=True,
                )
        except runtime.RuntimeControlError as exc:
            raise DockerError(str(exc)) from exc
        except OSError as exc:
            _write_job(folder, {**info, "state": "failed", "finished": time.time()})
            raise DockerError("Could not launch the Docker operation") from exc
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _stop_process(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=4)


def cancel_all(name: str, *, timeout: float = 14) -> None:
    """Cancel and join the managed Compose job before project shutdown."""
    folder = _jobs_dir(name)
    current = _read_job(folder)
    if not current or current.get("state") not in ("queued", "running"):
        return
    (folder / "latest.cancel").touch(mode=0o600, exist_ok=True)
    # A worker that was queued but never launched can be invalidated.
    # run_job must recheck both identifier and queued/running admission.
    if current.get("state") == "queued":
        _write_job(folder, {**current, "state": "cancelled",
                            "finished": time.time(), "exit_code": None})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _read_job(folder)
        if state and state.get("state") not in ("queued", "running"):
            return
        time.sleep(.2)
    raise DockerError("Compose background work did not stop; runtime is not inactive")


def run_job(name: str, action: str, job_id: str) -> int:
    if action not in ACTIONS or not re.fullmatch(r"[0-9a-f]{16}", job_id):
        return 2
    folder = _jobs_dir(name)
    job = _read_job(folder)
    if not job or job.get("id") != job_id or job.get("action") != action or job.get("state") != "queued":
        return 2
    from . import runtime
    result_code = 1
    state = "failed"
    marker = folder / "latest.cancel"
    proc = None
    try:
        with (folder / "latest.log").open("ab") as log:
            with runtime.admit(name):
                latest = _read_job(folder)
                if (marker.exists() or latest is None or
                        latest.get("id") != job_id or latest.get("state") != "queued"):
                    state = "cancelled"
                else:
                    job["state"] = "running"
                    _write_job(folder, job)
                    proc = subprocess.Popen(
                        compose_command(name) + list(ACTIONS[action]),
                        cwd=project_root(name), stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True, close_fds=True,
                    )
            if proc is not None:
                deadline = time.monotonic() + MAX_JOB_SECONDS
                while proc.poll() is None:
                    if marker.exists() or not runtime.is_active(name):
                        state = "cancelled"
                        _stop_process(proc)
                        break
                    if time.monotonic() >= deadline:
                        state = "failed"
                        _stop_process(proc)
                        break
                    time.sleep(.2)
                else:
                    result_code = proc.returncode
                    state = "success" if result_code == 0 else "failed"
    except runtime.RuntimeControlError:
        state = "cancelled"
    except (OSError, subprocess.TimeoutExpired, DockerError):
        with (folder / "latest.log").open("ab") as log:
            log.write(b"\\nDocker operation failed or could not terminate cleanly.\\n")
    job["state"] = state
    job["finished"] = time.time()
    job["exit_code"] = result_code if state in ("success", "failed") else None
    _write_job(folder, job)
    return result_code



def smoke_fixture_dir() -> Path:
    """Locate the bundled fixture in the installed release or source checkout."""
    source = Path("/opt/devstack/current/source/examples/smoke")
    if not source.is_dir():
        source = Path(__file__).resolve().parents[2] / "examples/smoke"
    if not source.is_dir():
        raise DockerError("Bundled smoke fixture not found")
    return source


def import_smoke() -> str:
    """Copy bundled fixture into a disposable local project, not into Git."""
    from .project_setup import create_project
    source = smoke_fixture_dir()
    workspace = Path.home() / "workspaces"
    workspace.mkdir(exist_ok=True, mode=0o700)
    destination = workspace / "smoke"
    if destination.exists() or destination.is_symlink():
        raise DockerError("The smoke workspace already exists")
    if "smoke" in load_projects():
        raise DockerError("A smoke project is already registered")
    shutil.copytree(source, destination)
    try:
        (destination / ".devstack").chmod(0o700)
        create_project("smoke")
    except Exception:
        shutil.rmtree(destination)
        raise
    return "smoke"


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] != "--job":
        return 2
    try:
        return run_job(sys.argv[2], sys.argv[3], sys.argv[4])
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
