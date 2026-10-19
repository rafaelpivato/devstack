"""Read-only three-way review of Git template changes for a single project.

Git source and imported baselines are advisory inputs. SQLite application/task
definitions and managed Compose remain authoritative. No Git commands, Docker
actions, writes or implicit project switching occur during review.
"""
import hashlib
import json
from pathlib import Path

from devstack.domain.models import project_id
from devstack.projects.manifest import ManifestError, _apps, _tasks, parse_template


class SourceReviewInvalid(ValueError):
    pass


def _application(value):
    return {"name": value.name, "service": value.service, "port": value.port}


def _task(value):
    return {
        "name": value.name, "service": value.service, "mode": value.mode,
        "image": value.image, "network": value.network,
        "command": list(value.argv),
    }


class SourceReviewService:
    def __init__(self, registry, workspaces, provisioner, configuration):
        self.registry = registry
        self.workspaces = workspaces
        self.provisioner = provisioner
        self.configuration = configuration

    def _baseline(self, identifier: str, filename: str) -> bytes | None:
        folder = self.workspaces.path(identifier) / "baseline"
        if folder.is_symlink() or not folder.is_dir():
            raise SourceReviewInvalid("Invalid source baseline directory")
        file = folder / filename
        if file.is_symlink():
            raise SourceReviewInvalid("Symlinked source baseline is forbidden")
        if not file.exists():
            return None
        if not file.is_file() or file.stat().st_size > 128 * 1024:
            raise SourceReviewInvalid("Invalid source baseline")
        return file.read_bytes()

    @staticmethod
    def _items(kind: str, incoming: list[dict], original: list[dict],
               local: list[dict]) -> list[dict]:
        sources = {row["name"]: row for row in incoming}
        baselines = {row["name"]: row for row in original}
        managed = {row["name"]: row for row in local}
        result = []
        # Deletions from Git are informational. They never imply removal of
        # local definitions without a separate explicit destructive action.
        for name in sorted(set(sources) | set(baselines)):
            source = sources.get(name)
            baseline = baselines.get(name)
            current = managed.get(name)
            if source is None:
                if current is not None and baseline is not None:
                    result.append({
                        "id": kind + ":" + name, "kind": kind, "name": name,
                        "action": "removed_upstream", "locally_modified": current != baseline,
                        "selectable": False,
                    })
                continue
            if source == baseline or source == current:
                continue
            result.append({
                "id": kind + ":" + name, "kind": kind, "name": name,
                "action": "add" if current is None else "update",
                "locally_modified": current != baseline,
                "selectable": True,
            })
        return result

    def review(self, identifier: str) -> dict:
        project_id(identifier)
        metadata = self.registry.import_metadata(identifier)
        source = self.provisioner.source_directory(identifier)
        # Provisioner resolves the validated .devstack folder; the manifest
        # parser accepts the containing repository/template directory.
        template = parse_template(source.parent)
        baselines = {
            name: self._baseline(identifier, name)
            for name in ("project.toml", "tasks.toml", "compose.yaml")
        }
        if baselines["project.toml"] is None:
            raise SourceReviewInvalid("Imported project has no source baseline")
        try:
            original_apps = [_application(item) for item in _apps(baselines["project.toml"])]
            original_tasks = ([_task(item) for item in _tasks(baselines["tasks.toml"])]
                              if baselines["tasks.toml"] is not None else [])
        except (ManifestError, UnicodeError) as exc:
            raise SourceReviewInvalid("Imported baseline is invalid") from exc
        settings = self.configuration.settings(identifier)
        compose = self.configuration.compose(identifier)
        apps = [_application(item) for item in template.applications]
        tasks = [_task(item) for item in template.tasks]
        result = self._items("application", apps, original_apps, settings["applications"])
        result += self._items("task", tasks, original_tasks, settings["tasks"])

        new_compose = template.managed_files.get("compose.yaml")
        old_compose = baselines["compose.yaml"]
        managed_compose = (compose["content"].encode("utf-8")
                           if compose["revision"] != "none" else None)
        if old_compose is not None and new_compose is None:
            if managed_compose is not None:
                result.append({
                    "id": "compose.yaml", "kind": "compose", "name": "compose.yaml",
                    "action": "removed_upstream",
                    "locally_modified": managed_compose != old_compose,
                    "selectable": False,
                })
        elif new_compose is not None and new_compose != old_compose and new_compose != managed_compose:
            result.append({
                "id": "compose.yaml", "kind": "compose", "name": "compose.yaml",
                "action": "add" if managed_compose is None else "update",
                "locally_modified": managed_compose != old_compose,
                "selectable": True,
            })

        # The revision includes the complete bounded source, baseline and
        # managed configuration. Future selective apply must compare it while
        # holding the coordinator lock, before applying any changes.
        material = {
            "source": {key: value.hex() if value is not None else None
                       for key, value in template.managed_files.items()},
            "baseline": {key: value.hex() if value is not None else None
                         for key, value in baselines.items()},
            "managed": {"settings": settings, "compose": compose},
        }
        revision = hashlib.sha256(json.dumps(
            material, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {
            "project_id": identifier,
            "source_repository_id": metadata["repository_id"],
            "revision": revision,
            "changes": result,
        }
