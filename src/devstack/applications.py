"""Explicit project application registry. Docker dependencies are not applications.

Stored as a small local TOML file alongside project-managed Compose, never
inside cloned repositories. Registration only changes metadata; it does NOT
publish a port, create a proxy, or launch containers.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib

from .docker_stacks import DockerError, compose_command, compose_path, managed_dir, project_root


class ApplicationError(RuntimeError):
    """Invalid application registry or operation."""


@dataclass(frozen=True)
class Application:
    name: str
    service: str
    port: int | None


SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
MAX_REGISTRY_BYTES = 32 * 1024
MAX_APPLICATIONS = 30


def _valid_name(name: str) -> str:
    name = name.strip()
    if (not name or len(name) > 64 or "/" in name or "\\" in name
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        raise ApplicationError("Application name must be 1–64 printable characters without slashes")
    return name


def _valid_service(value: str) -> str:
    if not SERVICE_NAME.fullmatch(value) or ".." in value:
        raise ApplicationError("Use a Compose service name (letters, digits, dots, hyphens or underscores)")
    return value


def _valid_port(value: int | str | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ApplicationError("Container port must be between 1 and 65535")
    if isinstance(value, str) and (not value.isascii() or not value.isdecimal()):
        raise ApplicationError("Container port must be between 1 and 65535")
    try:
        port = int(value)
    except (ValueError, TypeError) as exc:
        raise ApplicationError("Container port must be between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ApplicationError("Container port must be between 1 and 65535")
    return port


def registry_path(project: str) -> Path:
    target = managed_dir(project) / "project.toml"
    if target.is_symlink():
        raise ApplicationError("Refusing to follow a symlinked project manifest")
    return target


def _parse(content: str) -> tuple[Application, ...]:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ApplicationError("Invalid project.toml syntax") from exc
    if set(data) - {"applications"} or not isinstance(data.get("applications", []), list):
        raise ApplicationError("Unsupported project.toml fields; edit the file manually first")
    if len(data.get("applications", [])) > MAX_APPLICATIONS:
        raise ApplicationError("Too many applications (maximum 30)")
    results = []
    names = set()
    for entry in data.get("applications", []):
        if not isinstance(entry, dict) or not {"name", "service"} <= set(entry) or set(entry) - {"name", "service", "port"}:
            raise ApplicationError("Applications require name and service fields")
        if not isinstance(entry["name"], str) or not isinstance(entry["service"], str):
            raise ApplicationError("Application names and services must be strings")
        name = _valid_name(entry["name"])
        service = _valid_service(entry["service"])
        if "port" in entry and type(entry["port"]) is not int:
            raise ApplicationError("Application port must be an integer")
        port = _valid_port(entry.get("port"))
        if name.casefold() in names:
            raise ApplicationError("Application names must be unique")
        names.add(name.casefold())
        results.append(Application(name, service, port))
    return tuple(results)


def load_applications(project: str) -> tuple[Application, ...]:
    path = registry_path(project)
    if not path.exists():
        return ()
    try:
        if path.stat().st_size > MAX_REGISTRY_BYTES:
            raise ApplicationError("Project manifest is too large")
        return _parse(path.read_text(encoding="utf-8"))
    except UnicodeError as exc:
        raise ApplicationError("Project manifest must be UTF-8") from exc


def _render(applications: tuple[Application, ...]) -> str:
    lines = ["# Explicit dashboard applications; does not publish any ports."]
    for app in applications:
        lines += ["", "[[applications]]",
                  "name = " + json.dumps(app.name, ensure_ascii=False),
                  "service = " + json.dumps(app.service)]
        if app.port is not None:
            lines += ["port = " + str(app.port)]
    return "\n".join(lines) + "\n"


def _edit(project: str, callback) -> None:
    folder = managed_dir(project)
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    lockfile = folder / ".project.lock"
    if lockfile.is_symlink():
        raise ApplicationError("Refusing to follow a symlinked project lock")
    with lockfile.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            before = load_applications(project)
            after = tuple(callback(before))
            if len(after) > MAX_APPLICATIONS:
                raise ApplicationError("Too many applications (maximum 30)")
            rendered = _render(after)
            # Confirm that our serializer round-trips before committing.
            if _parse(rendered) != after:
                raise ApplicationError("Unable to serialize application manifest")
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=folder, prefix=".project-",
                suffix=".toml", delete=False
            ) as stream:
                staged = Path(stream.name)
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                staged.chmod(0o600)
                os.replace(staged, registry_path(project))
            finally:
                staged.unlink(missing_ok=True)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def add_application(project: str, name: str, service: str, port: int | str | None = None) -> None:
    entry = Application(_valid_name(name), _valid_service(service.strip()), _valid_port(port))

    def add(before):
        if any(app.name.casefold() == entry.name.casefold() for app in before):
            raise ApplicationError("An application with that name already exists")
        return (*before, entry)

    _edit(project, add)


def update_application(project: str, previous_name: str, name: str,
                       service: str, port: int | str | None = None) -> None:
    changed = Application(_valid_name(name), _valid_service(service.strip()), _valid_port(port))

    def update(before):
        if not any(app.name == previous_name for app in before):
            raise ApplicationError("Application not found")
        if any(app.name != previous_name and app.name.casefold() == changed.name.casefold()
               for app in before):
            raise ApplicationError("An application with that name already exists")
        return tuple(changed if app.name == previous_name else app for app in before)

    _edit(project, update)


def remove_application(project: str, name: str) -> None:
    def remove(before):
        after = tuple(app for app in before if app.name != name)
        if len(after) == len(before):
            raise ApplicationError("Application not found")
        return after

    _edit(project, remove)


def compose_states(project: str) -> dict[str, str] | None:
    """Status of Compose *services*, queried once per project, not per app.

    None signals unavailable Docker. No Compose side effects, health probes,
    or network exposure are triggered. Supports JSON array and NDJSON output
    across Docker Compose versions.
    """
    try:
        path = compose_path(project)
        if not path.is_file():
            return None
        proc = subprocess.run(
            compose_command(project) + ["ps", "--all", "--format", "json"],
            cwd=project_root(project), text=True, capture_output=True,
            timeout=8, check=False,
        )
        if proc.returncode:
            return None
        output = proc.stdout.strip()
        if not output:
            rows = []
        else:
            try:
                parsed = json.loads(output)
                rows = parsed if isinstance(parsed, list) else [parsed]
            except ValueError:
                rows = [json.loads(line) for line in output.splitlines() if line.strip()]
        states = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("Service"), str):
                continue
            service = row["Service"]
            state = str(row.get("State", "")).lower()
            health = str(row.get("Health", "")).lower()
            if state == "running":
                status = "unhealthy" if health == "unhealthy" else "running"
            else:
                status = "stopped"
            # If a service has several replicas, report running when any
            # healthy/running instance exists, but preserve unhealthy warning.
            priority = {"stopped": 0, "running": 1, "unhealthy": 2}
            if service not in states or priority[status] > priority[states[service]]:
                states[service] = status
        return states
    except (DockerError, OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return None


def application_status(states: dict[str, str] | None, application: Application) -> str:
    if states is None:
        return "unavailable"
    return states.get(application.service, "stopped")
