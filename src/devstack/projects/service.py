"""Project creation and GitHub import orchestration.

The controller owns this service. A browser never calls it directly or gets
filesystem, subprocess, or Docker privileges. Every project is identified by
a new UUID and its checkout is isolated under that project's managed root.
"""
from pathlib import Path
from uuid import uuid4

from devstack.adapters.github import GitHubSource
from devstack.domain.models import display_name, slug
from devstack.storage.projects import ProjectConflict, ProjectRegistry
from .manifest import parse_template
from .workspaces import WorkspaceError, WorkspaceStore


class ProjectProvisionError(RuntimeError):
    pass


class ProjectProvisioner:
    def __init__(self, registry: ProjectRegistry, workspaces: WorkspaceStore,
                 github: GitHubSource | None = None):
        self.registry = registry
        self.workspaces = workspaces
        self.github = github or GitHubSource()

    def _publish(self, identifier: str) -> None:
        """Finalize only after the filesystem publish completes successfully."""
        self.workspaces.publish(identifier)
        # If DB finalization fails, the pending record and published workspace
        # stay intact for reconciliation: never delete a published worktree.
        self.registry.finalize_import(identifier)

    def create_empty(self, project_slug: str, name: str):
        slug(project_slug)
        display_name(name)
        identifier = str(uuid4())
        self.workspaces.prepare(identifier)
        reserved = False
        try:
            self.registry.reserve_empty(identifier, project_slug, name)
            reserved = True
            self._publish(identifier)
            return self.registry.get(identifier)
        except Exception:
            if not self.workspaces.root(identifier).exists():
                if reserved:
                    self.registry.abort_import(identifier)
                self.workspaces.discard_pending(identifier)
            raise

    def import_github(self, url: str, project_slug: str, name: str):
        """Stage clone/metadata before registering; import never starts Docker."""
        slug(project_slug)
        display_name(name)
        identifier = str(uuid4())
        staging = self.workspaces.prepare(identifier)
        reserved = False
        try:
            # The GitHub adapter owns all URL validation and authentication.
            # Clone into a new staging directory, not a registered project.
            from devstack.adapters.github import parse_github_url
            parsed = parse_github_url(url)
            checkout_path = self.workspaces.checkout_path(staging, parsed.repository)
            checkout = self.github.clone_and_resolve(url, checkout_path)
            if checkout.repository != parsed.repository:
                raise ProjectProvisionError("GitHub checkout does not match requested repository")
            expected = checkout_path / checkout.subdirectory if checkout.subdirectory else checkout_path
            if (checkout.manifest_dir.resolve() != expected.resolve()
                    or not checkout.manifest_dir.resolve().is_relative_to(checkout_path.resolve())):
                raise ProjectProvisionError("Manifest path escapes the checkout")
            template = parse_template(checkout.manifest_dir)
            self.workspaces.install_template(staging, template)
            self.registry.reserve_import(
                identifier, project_slug, name, parsed.repository,
                parsed.clone_url, checkout.branch, checkout.subdirectory,
                template.applications, template.tasks,
            )
            reserved = True
            self._publish(identifier)
            return self.registry.get(identifier)
        except Exception:
            if not self.workspaces.root(identifier).exists():
                if reserved:
                    self.registry.abort_import(identifier)
                self.workspaces.discard_pending(identifier)
            # If published but not finalized, retain both the directory and
            # pending journal for recovery. Never silently destroy source.
            raise

    def recover_published_imports(self) -> tuple[str, ...]:
        """Repair a crash after rename but before the final SQLite transaction.

        Incomplete staged workspaces remain pending for explicit operator
        inspection; never auto-delete a checkout or infer its validity.
        """
        recovered = []
        for entry in self.registry.pending_imports():
            root = self.workspaces.root(entry.id)
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                raise ProjectProvisionError("Unverifiable published workspace")
            for name in ("repos", "managed", "secrets", "logs", "baseline"):
                path = root / name
                if not path.is_dir() or path.is_symlink():
                    raise ProjectProvisionError("Incomplete published project workspace")
            # Empty projects have no template; imports must retain their
            # validated source snapshot and linked repository.
            try:
                with self.registry.database.transaction() as con:
                    source = con.execute(
                        "SELECT repository_id FROM project_sources WHERE project_id=?",
                        (entry.id,),
                    ).fetchone()
                if source is not None:
                    if not (root / "managed" / "project.toml").is_file():
                        raise ProjectProvisionError("Missing imported project manifest")
                    if not any((root / "repos").iterdir()):
                        raise ProjectProvisionError("Missing imported repository")
            except OSError as exc:
                raise ProjectProvisionError("Unable to verify published workspace") from exc
            self.registry.finalize_import(entry.id)
            recovered.append(entry.id)
        return tuple(recovered)

    def source_directory(self, identifier: str) -> Path:
        """Resolve the linked Git template for later reviewed source sync."""
        metadata = self.registry.import_metadata(identifier)
        repositories = self.registry.repositories(identifier)
        repository = next(
            (r for r in repositories if r.id == metadata["repository_id"]),
            None,
        )
        if repository is None:
            raise ProjectProvisionError("Linked GitHub repository not registered")
        checkout = self.workspaces.repository(identifier, repository.relative_path)
        selected = checkout
        if metadata["subdirectory"]:
            for part in metadata["subdirectory"].split("/"):
                selected = selected / part
                if selected.is_symlink():
                    raise ProjectProvisionError("Linked GitHub source contains symlink")
        source = selected / ".devstack"
        if (source.is_symlink() or not source.is_dir()
                or not source.resolve().is_relative_to(checkout.resolve())):
            raise ProjectProvisionError("Linked project source is unavailable")
        return source
