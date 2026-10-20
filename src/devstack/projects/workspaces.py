"""Private, UUID-addressed per-project workspaces.

Project paths are managed by the controller, never supplied by HTTP clients.
A pending directory may become visible only after the SQL reservation succeeds.
"""
from pathlib import Path
import os
import shutil

from devstack.domain.models import project_id, repo_name, repo_path
from .manifest import ProjectTemplate


class WorkspaceError(RuntimeError):
    pass


class WorkspaceStore:
    def __init__(self, base: Path):
        self.base = Path(base)
        if not self.base.is_absolute():
            raise WorkspaceError("Workspace base must be absolute")
        if any(path.is_symlink() for path in (self.base, *self.base.parents)):
            raise WorkspaceError("Workspace path cannot contain symlinks")
        self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.base.chmod(0o700)

    def root(self, identifier: str) -> Path:
        project_id(identifier)
        return self.base / identifier

    def pending(self, identifier: str) -> Path:
        project_id(identifier)
        return self.base / (".pending-" + identifier)

    def prepare(self, identifier: str) -> Path:
        pending = self.pending(identifier)
        final = self.root(identifier)
        if (pending.exists() or pending.is_symlink()
                or final.exists() or final.is_symlink()):
            raise WorkspaceError("Project staging directory already exists")
        pending.mkdir(mode=0o700)
        try:
            for name in ("repos", "managed", "secrets", "logs", "baseline"):
                (pending / name).mkdir(mode=0o700)
        except Exception:
            # This is a brand-new UUID-scoped directory, not a user workspace.
            shutil.rmtree(pending)
            raise
        return pending

    def checkout_path(self, staging: Path, repository: str) -> Path:
        repo_name(repository)
        if staging.parent != self.base or not staging.name.startswith(".pending-"):
            raise WorkspaceError("Checkout must be inside a pending project workspace")
        path = staging / "repos" / repository
        if path.exists() or path.is_symlink():
            raise WorkspaceError("Repository checkout already exists")
        return path

    def install_template(self, staging: Path, template: ProjectTemplate) -> None:
        if staging.parent != self.base or not staging.name.startswith(".pending-"):
            raise WorkspaceError("Template destination is not a pending workspace")
        for name, content in template.managed_files.items():
            if name not in ("project.toml", "tasks.toml", "compose.yaml"):
                raise WorkspaceError("Unexpected managed file")
            for destination in (staging / "managed" / name,
                                staging / "baseline" / name):
                if destination.exists() or destination.is_symlink():
                    raise WorkspaceError("Refusing existing managed configuration")
                with destination.open("xb") as output:
                    output.write(content)
                destination.chmod(0o600)

    def publish(self, identifier: str) -> Path:
        pending = self.pending(identifier)
        final = self.root(identifier)
        if (not pending.is_dir() or pending.is_symlink()
                or final.exists() or final.is_symlink()):
            raise WorkspaceError("Project workspace cannot be published")
        for name in ("repos", "managed", "secrets", "logs", "baseline"):
            candidate = pending / name
            if not candidate.is_dir() or candidate.is_symlink():
                raise WorkspaceError("Incomplete project staging layout")
        os.rename(pending, final)
        return final

    def path(self, identifier: str) -> Path:
        location = self.root(identifier)
        if not location.is_dir() or location.is_symlink():
            raise WorkspaceError("Project workspace is unavailable")
        return location

    def repository(self, identifier: str, relative: str) -> Path:
        relative = repo_path(relative)
        root = self.path(identifier)
        candidate = root.joinpath(*relative.split("/"))
        if (not candidate.is_dir() or candidate.is_symlink()
                or not candidate.resolve().is_relative_to(root.resolve())):
            raise WorkspaceError("Project repository unavailable or escapes workspace")
        return candidate

    def discard_pending(self, identifier: str) -> None:
        """Discard only a staging directory for an unpublished import."""
        pending = self.pending(identifier)
        if pending.is_symlink():
            raise WorkspaceError("Refusing symlinked staging directory")
        if pending.is_dir() and not self.root(identifier).exists():
            shutil.rmtree(pending)
