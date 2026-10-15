"""Import a conventional .devstack project from GitHub without editing Git files.

A repository root URL searches for exactly one .devstack/project.toml.
A GitHub /tree/<branch>/<path> URL selects a subdirectory explicitly.
The imported definition is copied to the project's managed .devstack folder;
the original template directory is retained as the Compose path base.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
from urllib.parse import urlsplit, unquote

from .core import config_path, load_projects
from .github_ssh import GitHubSSHError, git_environment, ssh_command
from .project_setup import (
    ProjectSetupError, _edit, _write_config, checked_name, github_ssh_url,
)
from .applications import ApplicationError, _parse as parse_applications

_GITHUB = re.compile(
    r"^/(?P<owner>[A-Za-z0-9-]{1,39})/"
    r"(?P<repo>[A-Za-z0-9_.-]{1,100})(?:/tree/(?P<tree>.+))?/?$"
)
_EXCLUDE = {".git", ".venv", "node_modules", ".tox", ".next",
            "dist", "build", "__pycache__", ".mypy_cache"}
_MAX_SCAN_DIRS = 2500
_MAX_SCAN_DEPTH = 7
_MAX_CONFIG = 128 * 1024


class ImportError(ProjectSetupError):
    """An import cannot be completed without changing existing projects."""


def parse_template_url(url: str) -> tuple[str, str, str | None]:
    """Return normalized SSH clone URL, repository name and optional tree path."""
    value = url.strip()
    if value.startswith(("git@github.com:", "ssh://git@github.com/")):
        ssh, repo = github_ssh_url(value)
        return ssh, repo, None
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "github.com"
            or parsed.query or parsed.fragment or parsed.username
            or parsed.password or "%" in parsed.path):
        raise ImportError("Use a GitHub HTTPS repository or /tree/branch/path URL")
    match = _GITHUB.fullmatch(parsed.path)
    if not match:
        raise ImportError("Use a GitHub HTTPS repository or /tree/branch/path URL")
    # Reuse Devstack's existing owner and repo validation.
    ssh, repo = github_ssh_url(
        f"https://github.com/{match.group('owner')}/{match.group('repo')}"
    )
    tree = match.group("tree")
    if tree is not None:
        tree = tree.rstrip("/")
        if not tree or any(part in ("", ".", "..") for part in tree.split("/")):
            raise ImportError("Invalid GitHub tree path")
    return ssh, repo, tree


def _command(args: list[str], *, env: dict | None = None,
             timeout: int = 30) -> str:
    try:
        proc = subprocess.run(
            args, env=env, check=False, capture_output=True,
            text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImportError("Git import command failed or timed out") from exc
    if proc.returncode:
        raise ImportError("Git import failed. Check GitHub SSH, URL and branch.")
    return proc.stdout.strip()


def resolve_tree(checkout: Path, tree: str | None) -> tuple[str, Path]:
    """Use the longest actual branch ref in a GitHub tree URL.

    Branches may contain '/', so split the suffix using remote refs rather
    than assuming its first component is the branch.
    """
    if tree is None:
        return "", checkout
    remote_refs = _command([
        "git", "-C", str(checkout), "for-each-ref",
        "--format=%(refname:strip=3)", "refs/remotes/origin",
    ])
    matches = [b for b in remote_refs.splitlines()
               if b not in ("", "HEAD") and (tree == b or tree.startswith(b + "/"))]
    if not matches:
        raise ImportError("GitHub branch not found in repository; verify the tree URL")
    branch = max(matches, key=len)
    # Switch only to a branch read from Git's actual remote refs.
    _command([
        "git", "-C", str(checkout), "switch", "--track",
        "-c", branch, "origin/" + branch,
    ])
    suffix = tree[len(branch):].lstrip("/")
    if not suffix:
        return branch, checkout
    components = suffix.split("/")
    if any(part in ("", ".", "..") for part in components):
        raise ImportError("Invalid repository subdirectory")
    selected = checkout.joinpath(*components)
    if not selected.is_dir() or not selected.resolve().is_relative_to(checkout.resolve()):
        raise ImportError("The requested directory does not exist in this branch")
    return branch, selected


def _candidate(folder: Path) -> Path | None:
    config_dir = folder / ".devstack"
    config = config_dir / "project.toml"
    if (folder.is_symlink() or config_dir.is_symlink() or config.is_symlink()
            or (config_dir / "compose.yaml").is_symlink()):
        raise ImportError("Refusing symlinks in the project configuration path")
    if config.is_file():
        return folder
    return None


def discover(checkout: Path, selected: Path, explicit: bool) -> Path:
    """Discover an unambiguous project config, or require a narrower URL."""
    if explicit:
        found = _candidate(selected)
        if found is None:
            raise ImportError("No .devstack/project.toml at the selected GitHub path")
        return found
    root = _candidate(checkout)
    if root is not None:
        return root
    candidates = []
    count = 0
    for base, dirs, _ in os.walk(checkout, topdown=True, followlinks=False):
        depth = len(Path(base).relative_to(checkout).parts)
        dirs[:] = [d for d in dirs if d not in _EXCLUDE and
                   not (Path(base) / d).is_symlink() and depth < _MAX_SCAN_DEPTH]
        count += 1
        if count > _MAX_SCAN_DIRS:
            raise ImportError("Repository too large for automatic project discovery; use a tree URL")
        if base == str(checkout):
            continue
        found = _candidate(Path(base))
        if found is not None:
            candidates.append(found)
            if len(candidates) > 1:
                raise ImportError("Multiple Devstack manifests found; use a GitHub tree URL")
    if not candidates:
        raise ImportError("No .devstack/project.toml found; provide a configured repo or path")
    return candidates[0]


def import_github_project(url: str, requested_name: str = "") -> tuple[str, str]:
    """Clone and register a repo-backed project with a local config snapshot.

    The Git checkout is placed below the project root. Only after a successful
    clone, config discovery and validation do we register the new project.
    A failure rolls back the staged workspace and avoids partial registration.
    """
    ssh, repo, tree = parse_template_url(url)
    if repo in (".devstack", ".secrets"):
        raise ImportError("Repository name conflicts with Devstack-managed directories")
    try:
        env = git_environment()
        command = ssh_command()
    except GitHubSSHError as exc:
        raise ImportError(str(exc)) from exc
    parent = Path.home() / "workspaces"
    if parent.is_symlink():
        raise ImportError("Workspaces directory must not be a symlink")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".devstack-import-", dir=parent) as temp:
        checkout = Path(temp) / "checkout"
        _command(["git", "clone", "--", ssh, str(checkout)],
                 env=env, timeout=240)
        _command(["git", "-C", str(checkout), "config",
                  "--local", "core.sshCommand", command], timeout=15)
        branch, selected = resolve_tree(checkout, tree)
        template = discover(checkout, selected, explicit=tree is not None)
        source_dir = template / ".devstack"
        app_file = source_dir / "project.toml"
        compose_file = source_dir / "compose.yaml"
        if app_file.stat().st_size > _MAX_CONFIG:
            raise ImportError("Project manifest too large")
        content = app_file.read_text(encoding="utf-8")
        try:
            parse_applications(content)
        except ApplicationError as exc:
            raise ImportError("Invalid project template: " + str(exc)) from exc
        compose = None
        if compose_file.is_file():
            if compose_file.stat().st_size > _MAX_CONFIG:
                raise ImportError("Compose file too large")
            compose = compose_file.read_text(encoding="utf-8")
        suggested = template.name if template != checkout else repo
        name = checked_name(requested_name.strip() or suggested)
        relative = template.relative_to(checkout)
        # No new files are introduced into the cloned Git working tree.
        # Relative contexts must continue resolving against source .devstack.
        from .docker_stacks import imported_compose_arguments
        if compose is not None:
            args = imported_compose_arguments(
                "devstack-" + name, compose_file, source_dir
            ) + ["config", "--quiet"]
            try:
                result = subprocess.run(args, cwd=checkout, text=True,
                                        capture_output=True, timeout=25, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ImportError("Docker Compose validation unavailable") from exc
            if result.returncode:
                raise ImportError("Template Compose validation failed: " +
                                  (result.stderr or result.stdout).strip()[:500])

        def edit(config_path: Path, data: dict):
            if any(p["name"] == name for p in data.setdefault("projects", [])):
                raise ImportError("That project already exists; choose another project name")
            dest = parent / name
            if dest.exists() or dest.is_symlink():
                raise ImportError("Project workspace already exists; choose another name")
            try:
                dest.mkdir(mode=0o700)
                target = dest / repo
                shutil.move(str(checkout), target)
                managed = dest / ".devstack"
                managed.mkdir(mode=0o700)
                (dest / ".secrets").mkdir(mode=0o700)
                (managed / "project.toml").write_text(content, encoding="utf-8")
                (managed / "project.toml").chmod(0o600)
                if compose is not None:
                    (managed / "compose.yaml").write_text(compose, encoding="utf-8")
                    (managed / "compose.yaml").chmod(0o600)
                    import_meta = (
                        "[origin]\n"
                        f"repository = {__import__('json').dumps(repo)}\n"
                        f"subdirectory = {__import__('json').dumps(relative.as_posix() if relative != Path('.') else '')}\n"
                    )
                    (managed / "import.toml").write_text(import_meta, encoding="utf-8")
                    (managed / "import.toml").chmod(0o600)
                data["projects"].append({
                    "name": name,
                    "repos": [{"name": repo, "path": str(target)}],
                    "services": [],
                })
                _write_config(config_path, data)
            except Exception:
                if dest.exists() and not dest.is_symlink():
                    shutil.rmtree(dest)
                raise

        _edit(edit)
    return name, branch
