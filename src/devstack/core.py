"""Git and legacy service operations; no HTTP dependencies."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .workspace import BRANCH, NAME, Repo, Service, Project, config_path, load_project, load_projects


class OperationError(RuntimeError):
    """A configured operation failed."""


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
    # Legacy services have no reliable Docker ownership or cancellation model.
    # They cannot satisfy single-runtime isolation and are unsupported in v0.5.
    raise OperationError("Legacy service execution is disabled; use the managed Compose stack")


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
