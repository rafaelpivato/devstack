"""Project configuration and explicitly allowlisted Linux operations."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")


class OperationError(RuntimeError):
    """A configured operation failed."""


@dataclass(frozen=True)
class Repo:
    name: str
    path: Path


@dataclass(frozen=True)
class Service:
    name: str
    kind: str
    cwd: Path | None = None
    compose_file: str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class Project:
    name: str
    repos: tuple[Repo, ...]
    services: tuple[Service, ...]


def _name(value: str) -> str:
    if not NAME.fullmatch(value):
        raise ValueError(f"Invalid name: {value!r}")
    return value


def _path(value: str, base: Path) -> Path:
    path = Path(os.path.expanduser(value))
    return (path if path.is_absolute() else base / path).resolve()


def config_path() -> Path:
    return Path(os.environ.get("DEVSTACK_CONFIG", "~/.config/devstack/projects.toml")).expanduser()


def load_projects(path: Path | None = None) -> dict[str, Project]:
    path = path or config_path()
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    result = {}
    for entry in data.get("projects", []):
        name = _name(entry["name"])
        if name in result:
            raise ValueError(f"Duplicate project: {name}")
        repos = tuple(Repo(_name(r["name"]), _path(r["path"], path.parent))
                      for r in entry.get("repos", []))
        services = []
        for s in entry.get("services", []):
            kind = s["kind"]
            if kind not in ("compose", "systemd"):
                raise ValueError(f"Unsupported service kind: {kind}")
            svc_name = _name(s["name"])
            cwd = _path(s["cwd"], path.parent) if "cwd" in s else None
            unit = s.get("unit")
            if kind == "compose" and cwd is None:
                raise ValueError(f"Compose service {svc_name} requires cwd")
            if kind == "systemd" and (not unit or not NAME.fullmatch(unit)):
                raise ValueError(f"Systemd service {svc_name} requires valid unit")
            compose_file = s.get("compose_file")
            if compose_file and (Path(compose_file).is_absolute() or ".." in Path(compose_file).parts):
                raise ValueError("compose_file must stay within cwd")
            services.append(Service(svc_name, kind, cwd, compose_file, unit))
        result[name] = Project(name, repos, tuple(services))
    return result


def run(args: list[str], cwd: Path | None = None, timeout: int = 90) -> str:
    """Never invoke a shell; commands and working directories come from config."""
    try:
        proc = subprocess.run(
            args, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationError(str(exc)) from exc
    if proc.returncode:
        raise OperationError((proc.stderr or proc.stdout or "Command failed").strip()[:2000])
    return proc.stdout.strip()


def _compose(service: Service) -> list[str]:
    args = ["docker", "compose"]
    if service.compose_file:
        args += ["-f", service.compose_file]
    return args


def service_action(service: Service, action: str) -> None:
    if action not in ("start", "stop"):
        raise ValueError("Unsupported action")
    if service.kind == "compose":
        run(_compose(service) + (["up", "-d"] if action == "start" else ["down"]),
            cwd=service.cwd, timeout=180)
    else:
        run(["systemctl", "--user", action, service.unit], timeout=60)


def project_action(project: Project, action: str) -> None:
    services = project.services if action == "start" else reversed(project.services)
    for service in services:
        service_action(service, action)


def service_status(service: Service) -> str:
    try:
        if service.kind == "systemd":
            return run(["systemctl", "--user", "is-active", service.unit], timeout=8)
        output = run(_compose(service) + ["ps", "--status", "running", "--services"],
                     cwd=service.cwd, timeout=12)
        return "running" if output else "stopped"
    except OperationError:
        return "stopped or unavailable"


def service_logs(service: Service, tail: int = 100) -> str:
    tail = max(1, min(int(tail), 200))
    if service.kind == "compose":
        return run(_compose(service) + ["logs", "--no-color", "--tail", str(tail)],
                   cwd=service.cwd, timeout=20)
    return run(["journalctl", "--user", "-u", service.unit,
                "--no-pager", "-n", str(tail), "--output=short-iso"], timeout=20)


def repo_branch(repo: Repo) -> str:
    return run(["git", "-C", str(repo.path), "branch", "--show-current"], timeout=10) or "(detached)"


def repo_dirty(repo: Repo) -> bool:
    return bool(run(["git", "-C", str(repo.path), "status", "--porcelain"], timeout=10))


def repo_update(repo: Repo) -> str:
    if repo_dirty(repo):
        raise OperationError(f"{repo.name}: working tree is not clean")
    branch = repo_branch(repo)
    if branch == "(detached)":
        raise OperationError(f"{repo.name}: detached HEAD")
    # Avoid a merge commit and preserve the selected branch.
    run(["git", "-C", str(repo.path), "fetch", "origin"], timeout=90)
    run(["git", "-C", str(repo.path), "merge", "--ff-only", f"origin/{branch}"], timeout=30)
    return branch


def repo_switch(repo: Repo, branch: str) -> None:
    if not BRANCH.fullmatch(branch) or ".." in branch or "@{" in branch or branch.endswith((".", "/")):
        raise OperationError("Invalid branch name")
    if repo_dirty(repo):
        raise OperationError(f"{repo.name}: working tree is not clean")
    run(["git", "-C", str(repo.path), "fetch", "origin"], timeout=90)
    # No arbitrary command string is ever passed to the shell.
    run(["git", "-C", str(repo.path), "switch", branch], timeout=30)
