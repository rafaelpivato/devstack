"""Controller-owned, UUID-scoped inspection and fast-forward of Git checkouts.

No browser-supplied path, Git command, remote, branch or shell is executed.
Source updates never import managed configuration or start project workloads.
"""
import os
from pathlib import Path
import re
import subprocess

from devstack.domain.models import project_id


_HEAD = re.compile(r"[a-f0-9]{40,64}\\Z")


class RepositoryNotFound(LookupError):
    pass


class RepositoryGitInvalid(ValueError):
    pass


class RepositoryGitError(RuntimeError):
    pass


class RepositoryGitService:
    def __init__(self, registry, workspaces, coordinator):
        self.registry = registry
        self.workspaces = workspaces
        self.coordinator = coordinator

    def _checkout(self, identifier: str, repository_id: str) -> tuple[object, Path]:
        project_id(identifier)
        project_id(repository_id)
        repository = next(
            (item for item in self.registry.repositories(identifier)
             if item.id == repository_id), None,
        )
        if repository is None:
            raise RepositoryNotFound("Repository not registered for this project")
        checkout = self.workspaces.repository(identifier, repository.relative_path)
        gitdir = checkout / ".git"
        if (gitdir.is_symlink() or not gitdir.is_dir()
                or not gitdir.resolve().is_relative_to(checkout.resolve())):
            raise RepositoryGitError("Repository Git metadata is unavailable")
        return repository, checkout

    @staticmethod
    def _git(checkout: Path, *args: str, allow_missing: bool = False) -> str | None:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "/bin/false"
        try:
            result = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "-C", str(checkout), *args],
                check=False, capture_output=True, text=True, timeout=15, env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepositoryGitError("Git command unavailable or timed out") from exc
        if result.returncode:
            if allow_missing:
                return None
            # Git stderr often embeds private paths, hosts and credentials.
            raise RepositoryGitError("Git operation failed")
        return result.stdout.strip()

    def status(self, identifier: str, repository_id: str) -> dict:
        repo, checkout = self._checkout(identifier, repository_id)
        if self._git(checkout, "rev-parse", "--is-inside-work-tree") != "true":
            raise RepositoryGitError("Invalid Git working tree")
        head = self._git(checkout, "rev-parse", "--verify", "HEAD")
        branch = self._git(checkout, "branch", "--show-current")
        dirty = bool(self._git(checkout, "status", "--porcelain=v1", "--untracked-files=all"))
        upstream = self._git(
            checkout, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
            "@{upstream}", allow_missing=True,
        )
        ahead = behind = None
        if upstream:
            counts = self._git(checkout, "rev-list", "--left-right", "--count",
                               "HEAD...@{upstream}")
            try:
                ahead, behind = (int(n) for n in counts.split())
            except (ValueError, AttributeError) as exc:
                raise RepositoryGitError("Cannot inspect Git divergence") from exc
        return {
            "id": repo.id, "project_id": identifier, "name": repo.name,
            "branch": branch or None, "head": head, "upstream": upstream,
            "dirty": dirty, "ahead": ahead, "behind": behind,
        }

    def update(self, identifier: str, repository_id: str, expected_head: str) -> dict:
        if not isinstance(expected_head, str) or not _HEAD.fullmatch(expected_head):
            raise RepositoryGitInvalid("Expected a Git commit hash")
        # Serializing the full operation with activation and configuration
        # writers prevents source changes during an executing project.
        with self.coordinator.locked():
            state = self.coordinator.status()
            if state["phase"] != "inactive" or state["project_id"] is not None:
                raise RepositoryGitError("Deactivate before updating repository source")
            self.coordinator._verify_clear()
            current = self.status(identifier, repository_id)
            if current["head"] != expected_head:
                raise RepositoryGitError("Repository changed since inspection")
            if current["dirty"] or not current["branch"]:
                raise RepositoryGitError("Repository must be clean and on a branch")
            upstream = current["upstream"]
            if not upstream or not upstream.startswith("origin/"):
                raise RepositoryGitError("Git branch must track origin")
            _, checkout = self._checkout(identifier, repository_id)
            self._git(checkout, "fetch", "--no-tags", "origin")
            refreshed = self.status(identifier, repository_id)
            if (refreshed["head"] != expected_head or refreshed["dirty"]
                    or refreshed["branch"] != current["branch"]
                    or refreshed["upstream"] != upstream):
                raise RepositoryGitError("Repository changed during fetch")
            if refreshed["ahead"] and refreshed["behind"]:
                raise RepositoryGitError("Diverged history requires manual resolution")
            if refreshed["behind"]:
                self._git(checkout, "merge", "--ff-only", "@{upstream}")
            return self.status(identifier, repository_id)
