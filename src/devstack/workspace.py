"""The single configured DevStack workspace.

Configuration is deliberately independent of Flask, Docker and the dashboard.
There may be no workspace yet, or exactly one. This is a breaking v0.5 schema.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")


@dataclass(frozen=True)
class Repo:
    name: str
    path: Path


@dataclass(frozen=True)
class Service:
    # Legacy service metadata, retained until the runtime rewrite in PR 02.
    name: str
    kind: str
    cwd: Path | None = None
    compose_file: str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class Project:
    name: str
    repos: tuple[Repo, ...]
    services: tuple[Service, ...] = ()


def _name(value: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value) or value in (".", ".."):
        raise ValueError(f"Invalid name: {value!r}")
    return value


def _path(value: str, base: Path) -> Path:
    if not isinstance(value, str):
        raise ValueError("Path must be a string")
    path = Path(os.path.expanduser(value))
    return (path if path.is_absolute() else base / path).resolve()


def config_path() -> Path:
    return Path(os.environ.get("DEVSTACK_CONFIG", "~/.config/devstack/workspace.toml")).expanduser()


def load_project(path: Path | None = None) -> Project | None:
    """Load the sole workspace, rejecting arrays and unknown configuration.

    Empty configuration is a valid unprovisioned host. Old projects.toml is
    intentionally not migrated or interpreted.
    """
    path = path or config_path()
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("Workspace configuration must not be a symlink")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if not data:
        return None
    if set(data) != {"project"} or not isinstance(data["project"], dict):
        raise ValueError("Expected one [project] table in workspace.toml")
    entry = data["project"]
    if set(entry) - {"name", "repos", "services"}:
        raise ValueError("Unknown workspace settings")
    name = _name(entry["name"])
    raw_repos = entry.get("repos", [])
    raw_services = entry.get("services", [])
    if not isinstance(raw_repos, list) or not isinstance(raw_services, list):
        raise ValueError("Repositories and services must be arrays")
    if raw_services:
        raise ValueError("Legacy services are not supported in v0.5; use managed Docker Compose")
    repos = []
    seen = set()
    for r in raw_repos:
        if not isinstance(r, dict) or set(r) != {"name", "path"}:
            raise ValueError("Repository requires name and path")
        repo_name = _name(r["name"])
        if repo_name in seen:
            raise ValueError("Duplicate repository")
        seen.add(repo_name)
        repos.append(Repo(repo_name, _path(r["path"], path.parent)))
    services = []
    for s in raw_services:
        if not isinstance(s, dict) or set(s) - {"name", "kind", "cwd", "compose_file", "unit"}:
            raise ValueError("Invalid service settings")
        kind = s["kind"]
        if kind not in ("compose", "systemd"):
            raise ValueError(f"Unsupported service kind: {kind}")
        svc_name = _name(s["name"])
        cwd = _path(s["cwd"], path.parent) if "cwd" in s else None
        unit = s.get("unit")
        if kind == "compose" and cwd is None:
            raise ValueError(f"Compose service {svc_name} requires cwd")
        if kind == "systemd" and (not isinstance(unit, str) or not NAME.fullmatch(unit)):
            raise ValueError(f"Systemd service {svc_name} requires valid unit")
        compose_file = s.get("compose_file")
        if compose_file is not None and (
            not isinstance(compose_file, str) or Path(compose_file).is_absolute()
            or ".." in Path(compose_file).parts
        ):
            raise ValueError("compose_file must stay within cwd")
        services.append(Service(svc_name, kind, cwd, compose_file, unit))
    return Project(name, tuple(repos), tuple(services))


def load_projects(path: Path | None = None) -> dict[str, Project]:
    """Temporary adapter for v0.4 internal integrations, never multi-project."""
    project = load_project(path)
    return {project.name: project} if project else {}
