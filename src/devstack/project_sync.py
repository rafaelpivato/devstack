"""Safe, reviewable sync of version-controlled Devstack project configuration.

Only GitHub-added projects with import.toml have an authoritative source.
No source file or local configuration is overwritten merely by updating Git.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import difflib
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .applications import ApplicationError, _parse as parse_apps, _render as render_apps
from .command_runner import CommandError, _parse_tasks as parse_tasks, _render_tasks as render_tasks, _atomic_write
from .docker_stacks import DockerError, _imported_base, compose_command, managed_dir, project_root, save_compose

FILES = ("project.toml", "tasks.toml", "compose.yaml")
MAX_BYTES = 128 * 1024
FILE_KIND = {"project.toml": "Application", "tasks.toml": "Task", "compose.yaml": "Compose"}


class SyncError(RuntimeError):
    """Project configuration cannot be safely synchronized."""


@dataclass(frozen=True)
class Change:
    id: str
    kind: str
    name: str
    action: str
    local: str
    upstream: str
    locally_changed: bool
    recommended: bool


def source_directory(project: str) -> Path:
    """Only explicitly added GitHub projects are eligible for source sync."""
    try:
        path = _imported_base(project)
    except DockerError as exc:
        raise SyncError(str(exc)) from exc
    if path is None:
        raise SyncError("This project has no linked GitHub configuration source")
    if path.is_symlink():
        raise SyncError("Refusing a linked configuration directory")
    return path


def _read(path: Path) -> str | None:
    if path.is_symlink():
        raise SyncError("Refusing symlinked project configuration")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise SyncError("Project configuration is not a regular file or exceeds size limit")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SyncError("Project configuration is unreadable or invalid UTF-8") from exc


def _base_directory(project: str) -> Path:
    target = managed_dir(project) / ".sync-base"
    if target.is_symlink():
        raise SyncError("Refusing symlinked sync baseline")
    return target


def record_initial_baseline(managed: Path, originals: dict[str, str | None]) -> None:
    """Called only during creation of a *new* GitHub-linked project."""
    directory = managed / ".sync-base"
    directory.mkdir(mode=0o700, exist_ok=False)
    for filename in FILES:
        value = originals.get(filename)
        if value is not None:
            _atomic_write(directory / filename, value)


def _entries(content: str | None, kind: str) -> dict[str, object]:
    if content is None:
        return {}
    try:
        items = parse_apps(content) if kind == "project.toml" else parse_tasks(content)
    except (ApplicationError, CommandError) as exc:
        raise SyncError("Invalid " + kind + ": " + str(exc)) from exc
    return {item.name: item for item in items}


def _show(value: object) -> str:
    if value is None:
        return "(not present)"
    if isinstance(value, str):
        return value
    obj = asdict(value)
    if "argv" in obj:
        obj["command"] = obj.pop("argv")
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _diff(before: str, after: str) -> str:
    return "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="Local", tofile="From Git", lineterm=""
    ))[:15000]


def _snapshot(project: str):
    source = source_directory(project)
    managed = managed_dir(project)
    base = _base_directory(project)
    sources = {name: _read(source / name) for name in FILES}
    locals_ = {name: _read(managed / name) for name in FILES}
    baselines = {name: _read(base / name) for name in FILES}
    if sources["project.toml"] is None:
        raise SyncError("The source no longer contains .devstack/project.toml")
    encoded = json.dumps([sources, locals_, baselines], sort_keys=True).encode("utf-8")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    return sources, locals_, baselines, fingerprint


def review(project: str) -> tuple[list[Change], str]:
    sources, local, bases, fingerprint = _snapshot(project)
    changes = []
    for file in FILES[:2]:
        upstream = _entries(sources[file], file)
        existing = _entries(local[file], file)
        baseline = _entries(bases[file], file)
        for name, value in upstream.items():
            before = existing.get(name)
            if before == value:
                continue
            base = baseline.get(name)
            locally_changed = before is not None and (bases[file] is None or before != base)
            action = "add" if before is None else "update"
            changes.append(Change(
                id=f"{file}:{name}", kind=FILE_KIND[file], name=name,
                action=action, local=_show(before), upstream=_show(value),
                locally_changed=locally_changed,
                recommended=action == "add",
            ))
        # Never automatically remove locally present entries when source removes them.
    if sources["compose.yaml"] is not None and sources["compose.yaml"] != local["compose.yaml"]:
        before = local["compose.yaml"] or ""
        after = sources["compose.yaml"]
        locally_changed = local["compose.yaml"] is not None and (
            bases["compose.yaml"] is None or before != bases["compose.yaml"])
        changes.append(Change(
            id="compose.yaml", kind="Compose", name="compose.yaml",
            action="add" if local["compose.yaml"] is None else "update",
            local=_show(local["compose.yaml"]), upstream=_diff(before, after),
            locally_changed=locally_changed, recommended=False,
        ))
    return changes, fingerprint


def apply(project: str, selected: list[str], expected: str) -> int:
    """Apply explicit selections only; any upstream/local change invalidates review."""
    if len(selected) != len(set(selected)) or len(selected) > 100:
        raise SyncError("Invalid selection")
    folder = managed_dir(project)
    lock = folder / ".sync.lock"
    if lock.is_symlink():
        raise SyncError("Refusing a symlinked sync lock")
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        changes, actual = review(project)
        if actual != expected:
            raise SyncError("Source or local configuration changed; review differences again")
        known = {change.id for change in changes}
        if not set(selected) <= known:
            raise SyncError("Unknown sync changes selected")
        if not selected:
            return 0
        sources, local, bases, _ = _snapshot(project)
        pending = set(selected)
        writes: dict[str, str] = {}
        base_writes: dict[str, str] = {}
        for file in FILES[:2]:
            local_items = _entries(local[file], file)
            src_items = _entries(sources[file], file)
            base_items = _entries(bases[file], file)
            new_items = local_items.copy()
            new_base = base_items.copy()
            for name, value in src_items.items():
                if f"{file}:{name}" in pending:
                    new_items[name] = value
                    new_base[name] = value
            if new_items != local_items:
                value = (render_apps(tuple(new_items.values())) if file == "project.toml"
                         else render_tasks(tuple(new_items.values())))
                writes[file] = value
                base_writes[file] = (render_apps(tuple(new_base.values())) if file == "project.toml"
                                     else render_tasks(tuple(new_base.values())))
        if "compose.yaml" in pending:
            writes["compose.yaml"] = sources["compose.yaml"]
            base_writes["compose.yaml"] = sources["compose.yaml"]

        # Validate Compose before touching any other local configuration.
        if "compose.yaml" in writes:
            try:
                save_compose(project, writes["compose.yaml"])
            except DockerError as exc:
                raise SyncError("Compose validation failed: " + str(exc)) from exc
        for file in FILES[:2]:
            if file in writes:
                _atomic_write(folder / file, writes[file])
        baseline = _base_directory(project)
        baseline.mkdir(mode=0o700, exist_ok=True)
        for file in FILES:
            if file in base_writes:
                _atomic_write(baseline / file, base_writes[file])
        return len(selected)
