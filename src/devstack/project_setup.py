"""Atomic project configuration and GitHub-only repository onboarding."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib

from .core import BRANCH, NAME, config_path, load_projects
from .github_ssh import GitHubSSHError, git_environment, ssh_command

GITHUB_RE = re.compile(
    r"(?:(?:git@github\.com:)|(?:ssh://git@github\.com/)|(?:https://github\.com/))"
    r"(?P<owner>[A-Za-z0-9-]{1,39})/(?P<repo>[A-Za-z0-9_.-]{1,100})(?:\.git)?/?\Z"
)


class ProjectSetupError(RuntimeError):
    pass


def checked_name(name: str) -> str:
    if not NAME.fullmatch(name) or name in {".", ".."} or ".." in name:
        raise ProjectSetupError("Use letters, numbers, dots, underscores or hyphens")
    return name


def github_ssh_url(value: str) -> tuple[str, str]:
    match = GITHUB_RE.fullmatch(value.strip())
    if match is None:
        raise ProjectSetupError("Use a github.com SSH or HTTPS repository URL")
    owner, repo = match.group("owner"), match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if (not repo or repo in {".", ".."} or ".." in repo
            or owner.startswith("-") or owner.endswith("-")
            or repo.startswith("-")):
        raise ProjectSetupError("Invalid GitHub repository owner or name")
    return f"git@github.com:{owner}/{repo}.git", repo


def _serialized(config: dict) -> str:
    """Serialize the single-workspace schema without touching checkout files."""
    if set(config) - {"project"}:
        raise ProjectSetupError("Expected only one project object")
    project = config.get("project")
    lines = ["# DevStack v0.5: one workspace, multiple Git repositories."]
    if project is None:
        return "\n".join(lines) + "\n"
    if not isinstance(project, dict) or set(project) - {"name", "repos", "services"}:
        raise ProjectSetupError("Invalid workspace data")
    lines.extend(["", "[project]", f"name = {json.dumps(project['name'])}"])
    for repo in project.get("repos", []):
        if set(repo) != {"name", "path"}:
            raise ProjectSetupError("Invalid repository data")
        lines.extend(["", "[[project.repos]]",
                      f"name = {json.dumps(repo['name'])}",
                      f"path = {json.dumps(repo['path'])}"])
    for service in project.get("services", []):
        allowed = {"name", "kind", "cwd", "compose_file", "unit"}
        if set(service) - allowed:
            raise ProjectSetupError("Unknown legacy service fields")
        lines.extend(["", "[[project.services]]"])
        for key in ("name", "kind", "cwd", "compose_file", "unit"):
            if key in service:
                lines.append(f"{key} = {json.dumps(service[key])}")
    return "\n".join(lines) + "\n"

def _write_config(path: Path, data: dict) -> None:
    rendered = _serialized(data)
    # Make sure the serialization we control parses and passes existing validation.
    tomllib.loads(rendered)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".workspace-",
        delete=False
    ) as stream:
        staged = Path(stream.name)
        stream.write(rendered)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        staged.chmod(0o600)
        load_projects(staged)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _edit(callback) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ProjectSetupError("Refusing to edit a symlinked project config")
    lock = path.with_suffix(path.suffix + ".lock")
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if data and (set(data) != {"project"} or not isinstance(data["project"], dict)):
                raise ProjectSetupError("Invalid workspace format")
            load_projects(path)
            callback(path, data)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def create_project(name: str) -> None:
    name = checked_name(name)

    def edit(path: Path, data: dict) -> None:
        if "project" in data:
            raise ProjectSetupError("Only one project may be configured")
        workspace = Path.home() / "workspaces" / name
        if workspace.is_symlink():
            raise ProjectSetupError("Project workspace must not be a symlink")
        if workspace.exists():
            raise ProjectSetupError("Project workspace already exists")
        workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
        secrets = workspace / ".secrets"
        if secrets.is_symlink():
            raise ProjectSetupError("Project secrets directory must not be a symlink")
        secrets.mkdir(mode=0o700, exist_ok=True)
        secrets.chmod(0o700)
        data["project"] = {"name": name, "repos": [], "services": []}
        _write_config(path, data)

    _edit(edit)


def add_github_repository(project_name: str, remote: str, repo_name: str = "",
                          branch: str = "") -> str:
    project_name = checked_name(project_name)
    url, suggested = github_ssh_url(remote)
    repo_name = checked_name(repo_name.strip() or suggested)
    branch = branch.strip()
    if branch and (
        not BRANCH.fullmatch(branch) or ".." in branch or "@{" in branch
        or branch.endswith((".", "/"))
    ):
        raise ProjectSetupError("Invalid Git branch name")
    try:
        environment = git_environment()
        custom_ssh = ssh_command()
    except GitHubSSHError as exc:
        raise ProjectSetupError(str(exc)) from exc

    def edit(path: Path, data: dict) -> None:
        project = data.get("project")
        if project is not None and project["name"] != project_name:
            project = None
        if project is None:
            raise ProjectSetupError("Create the project first")
        if any(r["name"] == repo_name for r in project.get("repos", [])):
            raise ProjectSetupError("This repository name already exists in the project")
        workspace = Path.home() / "workspaces" / project_name
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Avoid following an existing link to a different checkout root.
        if workspace.is_symlink():
            raise ProjectSetupError("Workspace must not be a symlink")
        destination = workspace / repo_name
        if destination.exists() or destination.is_symlink():
            raise ProjectSetupError("Repository destination already exists")
        with tempfile.TemporaryDirectory(prefix=".clone-", dir=workspace) as temp:
            source = Path(temp) / "source"
            args = ["git", "clone"]
            if branch:
                args += ["--branch", branch]
            args += ["--", url, str(source)]
            try:
                subprocess.run(args, check=True, capture_output=True, text=True,
                               env=environment, timeout=240)
                subprocess.run(["git", "-C", str(source), "config",
                                "--local", "core.sshCommand", custom_ssh],
                               check=True, capture_output=True, timeout=15)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise ProjectSetupError(
                    "GitHub clone failed. Check SSH authorization, repo URL, "
                    "branch and any organization SSO requirement."
                ) from exc
            # Rename only after clone succeeds. Roll back if writing config fails.
            source.rename(destination)
        project.setdefault("repos", []).append(
            {"name": repo_name, "path": str(destination)}
        )
        try:
            _write_config(path, data)
        except Exception:
            import shutil
            shutil.rmtree(destination)
            raise

    _edit(edit)
    return repo_name
