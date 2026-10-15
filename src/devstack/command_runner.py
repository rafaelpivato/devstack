"""Individual Docker Compose commands, saved tasks and bounded execution history.

No host shell. Execution is isolated to the chosen Compose service, subject to
whatever host mounts and privileges the service declares. Trusted owner only.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import tomllib

from .docker_stacks import (
    DockerError, compose_command, compose_name, compose_path,
    managed_dir, project_root,
)

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,254}\Z")
IDENTIFIER = re.compile(r"[a-f0-9]{20}\Z")
TIMEOUT = 600
MAX_TASKS = 40
MAX_HISTORY = 30
MAX_TASK_FILE = 48 * 1024
MAX_COMMAND = 4096


class CommandError(RuntimeError):
    """A command cannot be safely started or its metadata is invalid."""


@dataclass(frozen=True)
class Task:
    name: str
    service: str
    mode: str
    argv: tuple[str, ...]
    image: str | None = None
    network: str = "none"


def _name(value: str, *, label: str = "Name") -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value) or ".." in value:
        raise CommandError(f"{label} must contain letters, numbers, dots, dashes or underscores")
    return value


def _mode(value: str) -> str:
    if value not in {"exec", "run"}:
        raise CommandError("Mode must be exec or run")
    return value


def _image(value: str) -> str:
    if (not isinstance(value, str) or not IMAGE.fullmatch(value)
            or ".." in value or "//" in value or value.endswith(("/", ":"))):
        raise CommandError("Enter a valid Docker image reference (e.g. python:3.12)")
    return value


def _network(value: str) -> str:
    if value not in ("none", "project"):
        raise CommandError("Network must be isolated or project")
    return value


def _task(name: str, service: str, mode: str, command, *,
          image: str = "", network: str = "none") -> Task:
    argv = _argv(command)
    if image:
        if service or mode != "run":
            raise CommandError("Standalone images require run mode and no Compose service")
        return Task(_name(name), "", "run", argv, _image(image), _network(network))
    if network != "none":
        raise CommandError("Project network option applies only to standalone images")
    return Task(_name(name), _name(service, label="Service"), _mode(mode), argv)


def _argv(value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        if len(value) > MAX_COMMAND:
            raise CommandError("Command is too long")
        try:
            args = shlex.split(value)
        except ValueError as exc:
            raise CommandError("Unbalanced command quotes") from exc
    elif isinstance(value, (tuple, list)):
        args = list(value)
    else:
        raise CommandError("Command must be a string or argument list")
    if not args or len(args) > 100 or any(
        not isinstance(x, str) or not x or "\x00" in x or len(x) > 1024
        for x in args
    ) or sum(len(x) for x in args) > MAX_COMMAND:
        raise CommandError("Enter a command with nonempty arguments (maximum 4 KiB)")
    return tuple(args)


def tasks_path(project: str) -> Path:
    target = managed_dir(project) / "tasks.toml"
    if target.is_symlink():
        raise CommandError("Refusing a symlinked task file")
    return target


def _parse_tasks(content: str) -> tuple[Task, ...]:
    try:
        parsed = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise CommandError("Invalid tasks.toml") from exc
    if set(parsed) - {"tasks"} or not isinstance(parsed.get("tasks", []), list):
        raise CommandError("Unsupported tasks.toml format")
    results = []
    seen = set()
    for entry in parsed.get("tasks", []):
        if not isinstance(entry, dict) or "name" not in entry or "command" not in entry:
            raise CommandError("Tasks require name and command")
        if not isinstance(entry["command"], list):
            raise CommandError("Saved commands must be argument arrays")
        if "image" in entry:
            if (set(entry) - {"name", "image", "network", "command"}
                    or "service" in entry or "mode" in entry):
                raise CommandError("Image tasks accept name, image, network and command")
            task = _task(entry["name"], "", "run", entry["command"],
                         image=entry["image"], network=entry.get("network", "none"))
        else:
            if set(entry) != {"name", "service", "mode", "command"}:
                raise CommandError("Service tasks require name, service, mode and command")
            task = _task(entry["name"], entry["service"], entry["mode"], entry["command"])
        if task.name in seen:
            raise CommandError("Duplicate task")
        seen.add(task.name)
        results.append(task)
    if len(results) > MAX_TASKS:
        raise CommandError("Too many saved tasks")
    return tuple(results)


def load_tasks(project: str) -> tuple[Task, ...]:
    path = tasks_path(project)
    if not path.exists():
        return ()
    if path.stat().st_size > MAX_TASK_FILE:
        raise CommandError("Task file is too large")
    return _parse_tasks(path.read_text(encoding="utf-8"))


def _render_tasks(tasks: tuple[Task, ...]) -> str:
    lines = ["# Commands run in containers; no implicit shell."]
    for task in tasks:
        lines += ["", "[[tasks]]", "name = " + json.dumps(task.name)]
        if task.image:
            lines += ["image = " + json.dumps(task.image)]
            if task.network != "none":
                lines += ["network = " + json.dumps(task.network)]
        else:
            lines += ["service = " + json.dumps(task.service),
                      "mode = " + json.dumps(task.mode)]
        lines += ["command = " + json.dumps(list(task.argv))]
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, value: str) -> None:
    if path.is_symlink():
        raise CommandError("Refusing a symlinked file")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".command-",
        delete=False,
    ) as stream:
        temp = Path(stream.name)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _directory(project: str) -> Path:
    folder = managed_dir(project)
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    return folder


def save_task(project: str, name: str, service: str, mode: str,
              command: str | list[str], *, image: str = "",
              network: str = "none") -> None:
    task = _task(name, service, mode, command, image=image, network=network)
    folder = _directory(project)
    lock = folder / ".tasks.lock"
    if lock.is_symlink():
        raise CommandError("Refusing symlinked task lock")
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        previous = load_tasks(project)
        result = tuple(t for t in previous if t.name != task.name) + (task,)
        if len(result) > MAX_TASKS:
            raise CommandError("Maximum 40 tasks")
        rendered = _render_tasks(result)
        if _parse_tasks(rendered) != result:
            raise CommandError("Task serialization failed")
        _atomic_write(tasks_path(project), rendered)


def delete_task(project: str, name: str) -> None:
    folder = _directory(project)
    lock = folder / ".tasks.lock"
    if lock.is_symlink():
        raise CommandError("Refusing symlinked task lock")
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        previous = load_tasks(project)
        updated = tuple(task for task in previous if task.name != name)
        if len(updated) == len(previous):
            raise CommandError("Task not found")
        _atomic_write(tasks_path(project), _render_tasks(updated))


def services(project: str) -> tuple[str, ...]:
    if not compose_path(project).is_file():
        raise CommandError("Configure Docker Compose first")
    try:
        proc = subprocess.run(
            compose_command(project) + ["config", "--services"],
            cwd=project_root(project), text=True, capture_output=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, DockerError) as exc:
        raise CommandError("Cannot read Compose services") from exc
    if proc.returncode:
        raise CommandError("Cannot read Compose services: " + proc.stderr.strip()[:200])
    names = tuple(line.strip() for line in proc.stdout.splitlines() if line.strip())
    if not names or len(names) > 100 or any(not NAME.fullmatch(x) for x in names):
        raise CommandError("Invalid Compose service listing")
    return names


def _history_dir(project: str) -> Path:
    folder = _directory(project) / "commands"
    if folder.is_symlink():
        raise CommandError("Refusing symlinked command directory")
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    return folder


def _paths(project: str, identifier: str) -> tuple[Path, Path, Path]:
    if not IDENTIFIER.fullmatch(identifier):
        raise CommandError("Invalid command identifier")
    folder = _history_dir(project)
    meta = folder / (identifier + ".json")
    log = folder / (identifier + ".log")
    cancel = folder / (identifier + ".cancel")
    if any(p.is_symlink() for p in (meta, log, cancel)):
        raise CommandError("Refusing command record symlink")
    return meta, log, cancel


def _record(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CommandError("Command record is unavailable") from exc


def get_job(project: str, identifier: str) -> dict:
    path, _, _ = _paths(project, identifier)
    entry = _record(path)
    if entry.get("id") != identifier:
        raise CommandError("Invalid command record")
    # Workers survive dashboard restarts; eventually flag abandoned jobs.
    if entry.get("state") in ("queued", "running") and (
        time.time() - float(entry.get("started", 0)) > TIMEOUT + 45
    ):
        return {**entry, "state": "stale"}
    return entry


def recent_jobs(project: str) -> list[dict]:
    folder = _history_dir(project)
    records = []
    for path in folder.glob("*.json"):
        if not IDENTIFIER.fullmatch(path.stem) or path.is_symlink():
            continue
        try:
            records.append(get_job(project, path.stem))
        except (CommandError, ValueError, TypeError):
            continue
    return sorted(records, key=lambda x: x.get("started", 0), reverse=True)[:MAX_HISTORY]


def job_output(project: str, identifier: str) -> str:
    _, log, _ = _paths(project, identifier)
    if not log.is_file():
        return ""
    with log.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - 48000))
        return stream.read().decode("utf-8", "replace")


def _prune(project: str) -> None:
    entries = recent_jobs(project)
    keep = {x["id"] for x in entries}
    folder = _history_dir(project)
    for path in folder.glob("*"):
        if path.suffix not in (".json", ".log", ".cancel") or path.stem in keep:
            continue
        if IDENTIFIER.fullmatch(path.stem) and not path.is_symlink():
            path.unlink(missing_ok=True)


def start_command(project: str, service: str, mode: str,
                  command: str | list[str], *, image: str = "",
                  network: str = "none") -> str:
    task = _task("manual", service, mode, command, image=image, network=network)
    if not task.image and task.service not in services(project):
        raise CommandError("Service is not defined by Docker Compose")
    if task.image and task.network == "project" and not compose_path(project).is_file():
        raise CommandError("A Compose stack is required for project network access")
    folder = _history_dir(project)
    lock = folder / ".lock"
    if lock.is_symlink():
        raise CommandError("Refusing symlinked command lock")
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if any(x["state"] in ("queued", "running") for x in recent_jobs(project)):
            raise CommandError("Another command is running in this project")
        identifier = secrets.token_hex(10)
        meta, log, _ = _paths(project, identifier)
        record = {"id": identifier, "service": task.service, "mode": task.mode,
                  "argv": list(task.argv), "state": "queued", "started": time.time()}
        if task.image:
            record["image"] = task.image
            record["network"] = task.network
        _atomic_write(meta, json.dumps(record))
        log.touch(mode=0o600)
        try:
            subprocess.Popen(
                [sys.executable, "-m", "devstack.command_runner",
                 "--worker", project, identifier],
                cwd=project_root(project), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, start_new_session=True,
            )
        except OSError as exc:
            _atomic_write(meta, json.dumps({**record, "state": "failed",
                                            "finished": time.time()}))
            raise CommandError("Could not start command worker") from exc
        _prune(project)
        return identifier


def start_task(project: str, name: str) -> str:
    task = next((t for t in load_tasks(project) if t.name == name), None)
    if task is None:
        raise CommandError("Saved task not found")
    return start_command(project, task.service, task.mode, list(task.argv),
                         image=task.image or "", network=task.network)


def cancel_command(project: str, identifier: str) -> None:
    entry = get_job(project, identifier)
    if entry.get("state") not in ("queued", "running"):
        raise CommandError("This command is no longer active")
    _, _, marker = _paths(project, identifier)
    marker.touch(mode=0o600, exist_ok=True)


def _project_network(project: str) -> str:
    """Find Compose's *default* network by labels; never infer its name."""
    if not compose_path(project).is_file():
        raise CommandError("Configure and start the Compose stack before using project networking")
    labels = [
        "label=com.docker.compose.project=" + compose_name(project),
        "label=com.docker.compose.network=default",
    ]
    try:
        proc = subprocess.run(
            ["docker", "network", "ls", "--quiet",
             "--filter", labels[0], "--filter", labels[1]],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError("Cannot inspect Docker networks") from exc
    if proc.returncode:
        raise CommandError("Cannot inspect Docker networks")
    matches = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{12,64}", matches[0]):
        raise CommandError(
            "The Compose default network is unavailable or ambiguous. "
            "Start the project's Compose stack first."
        )
    return matches[0]


def _terminate(proc: subprocess.Popen, *, run_container: str | None) -> None:
    # The Docker CLI may not relay signals to the process inside an existing
    # container. Stop the client, NOT the entire application container.
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    if run_container is not None:
        # Docker run --name ensures that temporary container can be removed.
        subprocess.run(["docker", "rm", "-f", run_container],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=False)


def work(project: str, identifier: str) -> int:
    meta, log, marker = _paths(project, identifier)
    entry = _record(meta)
    if entry.get("id") != identifier or entry.get("state") != "queued":
        return 2
    service, mode, argv = entry["service"], entry["mode"], _argv(entry["argv"])
    image = entry.get("image") or ""
    network = entry.get("network", "none")
    task = _task("worker", service, mode, list(argv), image=image, network=network)
    container_name = ("devstack-task-" + identifier) if (mode == "run" or image) else None
    result = 1
    status = "failed"
    entry["state"] = "running"
    _atomic_write(meta, json.dumps(entry))
    try:
        if marker.exists():
            status = "cancelled"
        else:
            with log.open("ab") as stream:
                if task.image:
                    target_network = _project_network(project) if task.network == "project" else "none"
                    args = [
                        "docker", "run", "--rm", "--name", container_name,
                        "--network", target_network,
                        "--security-opt", "no-new-privileges",
                        "--cap-drop", "ALL", task.image, *argv,
                    ]
                else:
                    tail = (["exec", "-T", service, *argv] if mode == "exec" else
                            ["run", "--rm", "--no-deps", "-T",
                             "--name", container_name, service, *argv])
                    args = compose_command(project) + tail
                proc = subprocess.Popen(
                    args,
                    cwd=project_root(project), stdin=subprocess.DEVNULL,
                    stdout=stream, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
                until = time.monotonic() + TIMEOUT
                while proc.poll() is None:
                    if marker.exists():
                        status = "cancelled"
                        _terminate(proc, run_container=container_name)
                        break
                    if time.monotonic() >= until:
                        status = "timed_out"
                        _terminate(proc, run_container=container_name)
                        break
                    time.sleep(.25)
                else:
                    result = proc.returncode
                    status = "success" if result == 0 else "failed"
    except (OSError, DockerError, CommandError, subprocess.TimeoutExpired):
        with log.open("ab") as stream:
            stream.write(b"\nCommand could not start or terminate cleanly.\n")
    entry["state"] = status
    entry["exit_code"] = result if status in ("success", "failed") else None
    entry["finished"] = time.time()
    _atomic_write(meta, json.dumps(entry))
    return result


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] != "--worker":
        return 2
    try:
        return work(sys.argv[2], sys.argv[3])
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
