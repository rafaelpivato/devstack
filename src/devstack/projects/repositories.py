"""Controller-owned, UUID-scoped inspection and fast-forward of Git checkouts.

No browser-supplied path, Git command, remote, branch or shell is executed.
Source updates never import managed configuration or start project workloads.
"""
import os
from pathlib import Path
import re
import subprocess
import hashlib
import json

from devstack.domain.models import project_id


_HEAD = re.compile(r"[a-f0-9]{40,64}\Z")



_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,199}\Z")


def _branch_name(name: str) -> str:
    """Accept only conservative Git branch refs, never revspecs or options."""
    if (not isinstance(name, str) or not _BRANCH.fullmatch(name)
            or ".." in name or "//" in name or name.endswith((".", "/"))
            or any(part in ("", ".", "..") or part.endswith(".lock")
                   for part in name.split("/"))):
        raise RepositoryGitInvalid("Invalid Git branch name")
    return name


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


    def _source_editable(self) -> None:
        state = self.coordinator.status()
        if state["phase"] != "inactive" or state["project_id"] is not None:
            raise RepositoryGitError("Deactivate the runtime before changing Git branches")
        self.coordinator._verify_clear()

    def _sync_editable(self, identifier: str) -> None:
        """Allow only the inactive runtime or the matching active owner.

        Caller holds the coordinator's global source-write lock. This is not
        the branch-switch permission: branch switching remains inactive-only.
        """
        project_id(identifier)
        state = self.coordinator.status()
        if state["phase"] == "active" and state["project_id"] == identifier:
            return
        if state["phase"] == "inactive" and state["project_id"] is None:
            self.coordinator._verify_clear()
            return
        raise RepositoryGitError("Repository update requires inactive or matching active project")

    @staticmethod
    def _ref(checkout: Path, ref: str) -> str | None:
        value = RepositoryGitService._git(
            checkout, "rev-parse", "--verify", ref + "^{commit}",
            allow_missing=True,
        )
        return value if value and _HEAD.fullmatch(value) else None

    def _origin_default(self, checkout: Path) -> str:
        """Verify the remote's advertised HEAD rather than assuming main/master."""
        response = self._git(checkout, "ls-remote", "--symref", "origin", "HEAD")
        matches = [
            line.split("\t", 1)[0][len("ref: refs/heads/"):]
            for line in response.splitlines()
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD")
        ]
        if len(matches) != 1:
            raise RepositoryGitError("Origin does not advertise one default branch")
        name = _branch_name(matches[0])
        if self._ref(checkout, "refs/remotes/origin/" + name) is None:
            raise RepositoryGitError("Origin default branch not fetched")
        return name

    def _branch_entry(self, identifier: str, repository, requested: str,
                      *, fetch: bool) -> dict:
        # Resolve checkout only from registry, never a client-supplied path.
        _, checkout = self._checkout(identifier, repository.id)
        before = self.status(identifier, repository.id)
        if fetch:
            self._git(checkout, "fetch", "--no-tags", "origin")
        current = self.status(identifier, repository.id)
        if (before["head"] != current["head"] or
                before["branch"] != current["branch"] or
                before["dirty"] != current["dirty"]):
            raise RepositoryGitError("Repository changed while inspecting branches")
        default = self._origin_default(checkout)
        target = requested if self._ref(
            checkout, "refs/remotes/origin/" + requested,
        ) is not None else default
        remote_head = self._ref(checkout, "refs/remotes/origin/" + target)
        local_head = self._ref(checkout, "refs/heads/" + target)
        if remote_head is None:
            raise RepositoryGitError("Verified target branch is unavailable")
        return {
            "repository_id": repository.id,
            "name": repository.name,
            "current_branch": current["branch"],
            "head": current["head"],
            "dirty": current["dirty"],
            "default_branch": default,
            "target_branch": target,
            "fallback": target != requested,
            "target_head": local_head or remote_head,
            "remote_head": remote_head,
            "local_exists": local_head is not None,
            "action": ("blocked" if current["dirty"] or not current["branch"]
                       else "already" if current["branch"] == target else "switch"),
        }

    @staticmethod
    def _revision(identifier: str, requested: str, scope: str | None,
                  entries: list[dict]) -> str:
        serialized = json.dumps(
            [identifier, requested, scope, entries],
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def branch_preview(self, identifier: str, requested: str,
                       repository_id: str | None = None) -> dict:
        project_id(identifier)
        _branch_name(requested)
        if repository_id is not None:
            project_id(repository_id)
        with self.coordinator.locked():
            self._source_editable()
            repositories = self.registry.repositories(identifier)
            if repository_id is not None:
                repositories = tuple(repo for repo in repositories
                                     if repo.id == repository_id)
                if not repositories:
                    raise RepositoryNotFound("Repository not registered for this project")
            if not repositories:
                raise RepositoryGitError("No repositories registered for project")
            entries = [self._branch_entry(
                identifier, repo, requested, fetch=True,
            ) for repo in repositories]
            return {
                "project_id": identifier, "requested_branch": requested,
                "repository_id": repository_id,
                "entries": entries,
                "ready": all(row["action"] != "blocked" for row in entries),
                "revision": self._revision(
                    identifier, requested, repository_id, entries,
                ),
            }

    def branch_apply(self, identifier: str, requested: str,
                     repository_id: str | None, revision: str) -> dict:
        project_id(identifier)
        _branch_name(requested)
        if repository_id is not None:
            project_id(repository_id)
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise RepositoryGitInvalid("Expected reviewed branch revision")
        with self.coordinator.locked():
            self._source_editable()
            repositories = self.registry.repositories(identifier)
            if repository_id is not None:
                repositories = tuple(repo for repo in repositories
                                     if repo.id == repository_id)
                if not repositories:
                    raise RepositoryNotFound("Repository not registered for this project")
            if not repositories:
                raise RepositoryGitError("No repositories registered for project")
            # Re-check every repo before changing *any*. No fetch here:
            # the preview pins remote-tracking refs and local worktree state,
            # and the revision covers all inputs.
            entries = [self._branch_entry(
                identifier, repo, requested, fetch=False,
            ) for repo in repositories]
            if self._revision(identifier, requested, repository_id, entries) != revision:
                raise RepositoryGitError("Branch preview is stale; review again")
            if any(row["action"] == "blocked" for row in entries):
                raise RepositoryGitError("A repository has local changes or detached HEAD")

            results = []
            for row in entries:
                try:
                    if row["action"] == "switch":
                        _, checkout = self._checkout(identifier, row["repository_id"])
                        target = row["target_branch"]
                        if row["local_exists"]:
                            self._git(checkout, "switch", "--", target)
                        else:
                            self._git(
                                checkout, "switch", "--track", "-c", target,
                                "origin/" + target,
                            )
                    after = self.status(identifier, row["repository_id"])
                    if after["branch"] != row["target_branch"]:
                        raise RepositoryGitError("Git did not reach reviewed branch")
                    results.append({
                        "repository_id": row["repository_id"], "name": row["name"],
                        "target_branch": row["target_branch"],
                        "fallback": row["fallback"], "status": "succeeded",
                        "head": after["head"],
                    })
                except RepositoryGitError:
                    # Already-completed repos remain switched; never reset or
                    # attempt an unsafe rollback. Return per-repo partial
                    # results so the operator can inspect and re-preview.
                    results.append({
                        "repository_id": row["repository_id"], "name": row["name"],
                        "target_branch": row["target_branch"],
                        "fallback": row["fallback"], "status": "failed",
                    })
                    break
            return {"project_id": identifier, "requested_branch": requested,
                    "results": results,
                    "complete": len(results) == len(entries)
                    and all(row["status"] == "succeeded" for row in results)}

    def update(self, identifier: str, repository_id: str, expected_head: str) -> dict:
        # This public operation preserves the one-project global source-write
        # boundary. Bulk updates share one coordinator lock across all repos.
        with self.coordinator.locked():
            return self.update_locked(identifier, repository_id, expected_head)

    def update_locked(self, identifier: str, repository_id: str,
                      expected_head: str) -> dict:
        """Fast-forward while the caller owns the global coordinator lock."""
        if not isinstance(expected_head, str) or not _HEAD.fullmatch(expected_head):
            raise RepositoryGitInvalid("Expected a Git commit hash")
        self._sync_editable(identifier)
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
