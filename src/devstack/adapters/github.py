"""Validated GitHub source URLs and read-only template discovery.

Source retrieval is an infrastructure adapter. It does not register projects,
alter workspaces outside the supplied staging checkout, or activate Docker.
"""
from dataclasses import dataclass
from pathlib import Path
import os
import re
import subprocess
from urllib.parse import urlsplit

from devstack.domain.models import repo_name
from devstack.github_ssh import GitHubSSHError, git_environment, ssh_command

_URL = re.compile(
    r"/(?P<owner>[A-Za-z0-9-]{1,39})/"
    r"(?P<repo>[A-Za-z0-9_.-]{1,100})(?:/tree/(?P<tree>.+))?/?\Z"
)
_SSH = re.compile(
    r"(?:git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[A-Za-z0-9-]{1,39})/(?P<repo>[A-Za-z0-9_.-]{1,100})\Z"
)
_SKIP = {".git", "node_modules", ".venv", ".tox", "dist", "build",
         ".next", "__pycache__", ".mypy_cache"}


class GitHubSourceError(RuntimeError):
    """Source URL, Git checkout or manifest discovery is invalid."""


@dataclass(frozen=True, slots=True)
class GitHubLocation:
    clone_url: str
    repository: str
    tree: str | None


@dataclass(frozen=True, slots=True)
class GitHubCheckout:
    clone_url: str
    repository: str
    branch: str
    subdirectory: str
    manifest_dir: Path


def parse_github_url(value: str) -> GitHubLocation:
    if not isinstance(value, str):
        raise GitHubSourceError("Expected a GitHub repository URL")
    value = value.strip()
    match = _SSH.fullmatch(value)
    if match:
        owner, repo = match.group("owner"), match.group("repo")
        repo = repo.removesuffix(".git")
        if owner.startswith("-") or owner.endswith("-"):
            raise GitHubSourceError("Invalid GitHub owner")
        repo_name(repo)
        if repo.startswith("-"):
            raise GitHubSourceError("Invalid repository name")
        return GitHubLocation(f"git@github.com:{owner}/{repo}.git", repo, None)
    url = urlsplit(value)
    if (url.scheme != "https" or url.netloc != "github.com"
            or url.query or url.fragment or url.username or url.password
            or "%" in url.path):
        raise GitHubSourceError("Use a github.com HTTPS or SSH URL")
    match = _URL.fullmatch(url.path)
    if not match:
        raise GitHubSourceError("Invalid GitHub repository or tree URL")
    owner, repo = match.group("owner"), match.group("repo").removesuffix(".git")
    if owner.startswith("-") or owner.endswith("-") or repo.startswith("-"):
        raise GitHubSourceError("Invalid GitHub owner or repository")
    repo_name(repo)
    tree = match.group("tree")
    if tree is not None:
        tree = tree.rstrip("/")
        if not tree or any(p in ("", ".", "..") for p in tree.split("/")):
            raise GitHubSourceError("Invalid branch/path suffix")
    return GitHubLocation(f"https://github.com/{owner}/{repo}.git", repo, tree)


def _git(argv: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(argv, env=env, check=False, timeout=240,
                                text=True, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubSourceError("Git command unavailable or timed out") from exc
    if result.returncode:
        raise GitHubSourceError("Git operation failed; verify repository access and URL")
    return result.stdout.strip()


def _manifest(directory: Path) -> bool:
    config = directory / ".devstack"
    if (directory.is_symlink() or config.is_symlink()
            or (config / "project.toml").is_symlink()
            or (config / "compose.yaml").is_symlink()
            or (config / "tasks.toml").is_symlink()):
        raise GitHubSourceError("Symlinked manifest path is forbidden")
    return (config / "project.toml").is_file()


def discover_manifest(checkout: Path, selected: Path, *, explicit: bool) -> Path:
    root = checkout.resolve(strict=True)
    if not selected.resolve(strict=True).is_relative_to(root):
        raise GitHubSourceError("Template directory escapes Git checkout")
    if explicit:
        if not _manifest(selected):
            raise GitHubSourceError("Selected GitHub folder has no .devstack/project.toml")
        return selected
    if _manifest(root):
        return root
    matches = []
    count = 0
    for current, subdirs, _ in os.walk(root, followlinks=False):
        directory = Path(current)
        depth = len(directory.relative_to(root).parts)
        subdirs[:] = [d for d in subdirs if d not in _SKIP
                      and not (directory / d).is_symlink() and depth < 7]
        count += 1
        if count > 2500:
            raise GitHubSourceError("Too many directories; use a precise tree URL")
        if directory != root and _manifest(directory):
            matches.append(directory)
            if len(matches) > 1:
                raise GitHubSourceError("Multiple manifests; select a GitHub tree URL")
    if not matches:
        raise GitHubSourceError("Repository has no .devstack/project.toml")
    return matches[0]


class GitHubSource:
    def clone_and_resolve(self, url: str, destination: Path) -> GitHubCheckout:
        location = parse_github_url(url)
        if destination.exists() or destination.is_symlink():
            raise GitHubSourceError("Clone destination already exists")
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "/bin/false"
        is_ssh = location.clone_url.startswith("git@github.com:")
        if is_ssh:
            try:
                env.update(git_environment())
                command = ssh_command()
            except GitHubSSHError as exc:
                raise GitHubSourceError(str(exc)) from exc
        _git(["git", "clone", "--", location.clone_url, str(destination)], env=env)
        if is_ssh:
            _git(["git", "-C", str(destination), "config",
                  "--local", "core.sshCommand", command])
        tree = location.tree
        branch = ""
        selected = destination
        if tree is not None:
            refs = _git(["git", "-C", str(destination), "for-each-ref",
                         "--format=%(refname:strip=3)", "refs/remotes/origin"])
            matches = [ref for ref in refs.splitlines()
                       if ref not in ("", "HEAD") and
                       (tree == ref or tree.startswith(ref + "/"))]
            if not matches:
                raise GitHubSourceError("Requested branch not found")
            branch = max(matches, key=len)
            active = _git(["git", "-C", str(destination), "branch", "--show-current"])
            if active != branch:
                _git(["git", "-C", str(destination), "switch", "--track",
                      "-c", branch, "origin/" + branch])
            suffix = tree[len(branch):].lstrip("/")
            if suffix:
                selected = destination.joinpath(*suffix.split("/"))
                if not selected.is_dir() or selected.is_symlink():
                    raise GitHubSourceError("Selected directory does not exist")
        manifest_dir = discover_manifest(destination, selected, explicit=tree is not None)
        return GitHubCheckout(location.clone_url, location.repository, branch,
                              manifest_dir.relative_to(destination).as_posix()
                              if manifest_dir != destination else "",
                              manifest_dir)
