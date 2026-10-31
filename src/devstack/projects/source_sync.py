"""Read-only three-way review of Git template changes for a single project.

Git source and imported baselines are advisory inputs. SQLite application/task
definitions and managed Compose remain authoritative. No Git commands, Docker
actions, writes or implicit project switching occur during review.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from devstack.domain.models import project_id
from devstack.projects.manifest import ManifestError, _apps, _tasks, parse_template


class SourceReviewInvalid(ValueError):
    pass


class SourceSyncConflict(RuntimeError):
    """A stale review, unsafe mutation or unreconciled sync blocks changes."""


_REVISION = re.compile(r"[a-f0-9]{64}" + "$")
_ALLOWED_FILES = {
    "managed/project.toml", "managed/tasks.toml", "managed/compose.yaml",
    "baseline/project.toml", "baseline/tasks.toml", "baseline/compose.yaml",
}


def _application(value):
    return {"name": value.name, "url": value.url} if value.url is not None else {
        "name": value.name, "service": value.service, "port": value.port,
    }


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

    @staticmethod
    def _render(kind: str, entries: dict[str, dict]) -> bytes:
        """Canonical private snapshot, validated with the shared importer parser."""
        rows = []
        for name in sorted(entries):
            data = entries[name]
            rows.append("[[" + kind + "]]")
            if kind == "applications":
                keys = ("name", "url") if "url" in data else ("name", "service", "port")
            else:
                keys = ("name", "service", "mode", "image", "network", "command")
            for key in keys:
                value = data.get(key)
                if value is None or (key in ("service", "image") and not value):
                    continue
                if key == "command":
                    rows.append("command = [" + ", ".join(
                        json.dumps(arg, ensure_ascii=True) for arg in value
                    ) + "]")
                elif key == "port":
                    rows.append("port = " + str(value))
                else:
                    rows.append(key + " = " + json.dumps(value, ensure_ascii=True))
        blob = ("\n".join(rows) + "\n").encode("utf-8") if rows else (
            kind + "=[]\n"
        ).encode("utf-8")
        if kind == "applications":
            _apps(blob)
        else:
            _tasks(blob)
        return blob

    def _compose_check(self, identifier: str, data: bytes) -> None:
        if len(data) > 48 * 1024 or not data or b"\x00" in data:
            raise SourceReviewInvalid("Imported Compose definition is too large or invalid")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceReviewInvalid("Imported Compose must be UTF-8") from exc
        folder = self.configuration._managed(identifier)
        source = self.provisioner.source_directory(identifier)
        from devstack.control.docker import _execute, compose_id
        with tempfile.NamedTemporaryFile(
            dir=folder, mode="wb", prefix=".sync-validate-", delete=False,
        ) as output:
            preview = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            _execute([
                "docker", "compose", "-p", compose_id(identifier),
                "--project-directory", str(source), "-f", str(preview),
                "config", "--quiet",
            ], 50)
        finally:
            preview.unlink(missing_ok=True)

    def _publish(self, identifier: str, payload: str) -> None:
        """Idempotently replay only frozen, allowlisted file snapshots.

        The durable SQLite journal and authoritative settings must already be
        committed. A crash at any rename leaves the journal for restart replay.
        """
        try:
            journal = json.loads(payload)
            files = journal["files"]
            if (not isinstance(files, dict) or not files
                    or not set(files).issubset(_ALLOWED_FILES)):
                raise SourceSyncConflict("Unexpected source sync journal files")
            root = self.workspaces.path(identifier)
            for relative in sorted(files):
                part, filename = relative.split("/")
                folder = root / part
                target = folder / filename
                if (folder.is_symlink() or not folder.is_dir()
                        or target.is_symlink()
                        or (target.exists() and not target.is_file())):
                    raise SourceSyncConflict("Unsafe source sync destination")
                data = base64.b64decode(files[relative], validate=True)
                if len(data) > 128 * 1024:
                    raise SourceSyncConflict("Oversized source sync journal")
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=folder, mode="wb", prefix=".sync-write-", delete=False,
                    ) as stream:
                        temporary = Path(stream.name)
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, target)
                    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SourceSyncConflict("Source sync publication needs recovery") from exc

    def recover_pending(self) -> int:
        """Complete an interrupted SQLite+filesystem publication before serving."""
        with self.configuration.coordinator.locked():
            state = self.configuration.coordinator.status()
            if state["phase"] != "inactive" or state["project_id"] is not None:
                raise SourceSyncConflict("Cannot replay source changes during runtime execution")
            # Verify real Docker ownership before replaying any files.
            # This bypasses *only* the pending-journal admission barrier,
            # never Docker/resource cleanup verification.
            self.configuration.coordinator._verify_clear(allow_pending_sync=True)
            with self.registry.database.transaction() as con:
                pending = con.execute(
                    "SELECT project_id,payload FROM source_sync_pending "
                    "ORDER BY started_ns,project_id",
                ).fetchall()
            for entry in pending:
                self._publish(entry["project_id"], entry["payload"])
                with self.registry.database.transaction(write=True) as con:
                    con.execute(
                        "DELETE FROM source_sync_pending WHERE project_id=?",
                        (entry["project_id"],),
                    )
            return len(pending)

    def apply(self, identifier: str, revision: str,
              selected: list[str], overwrites: list[str]) -> dict:
        project_id(identifier)
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise SourceReviewInvalid("Invalid review revision")
        if (not isinstance(selected, list) or not 1 <= len(selected) <= 80
                or any(not isinstance(item, str) or len(item) > 160 for item in selected)
                or len(set(selected)) != len(selected)):
            raise SourceReviewInvalid("Select unique source changes")
        if (not isinstance(overwrites, list)
                or any(not isinstance(item, str) for item in overwrites)
                or len(set(overwrites)) != len(overwrites)):
            raise SourceReviewInvalid("Invalid overwrite confirmation")

        with self.configuration.coordinator.locked():
            self.configuration._editable(identifier)
            review = self.review(identifier)
            if review["revision"] != revision:
                raise SourceSyncConflict("Source or managed configuration changed since review")
            choices = {item["id"]: item for item in review["changes"]
                       if item["selectable"]}
            if any(item not in choices for item in selected):
                raise SourceReviewInvalid("Unknown or non-selectable source change")
            required = {item for item in selected if choices[item]["locally_modified"]}
            if set(overwrites) != required:
                raise SourceSyncConflict("Explicit approval required for each local overwrite")

            source = self.provisioner.source_directory(identifier)
            template = parse_template(source.parent)
            settings = self.configuration.settings(identifier)
            baseline_apps = {row["name"]: row for row in
                             [_application(item) for item in
                              _apps(self._baseline(identifier, "project.toml"))]}
            baseline_tasks_raw = self._baseline(identifier, "tasks.toml")
            baseline_tasks = {row["name"]: row for row in (
                [_task(item) for item in _tasks(baseline_tasks_raw)]
                if baseline_tasks_raw is not None else []
            )}
            source_apps = {row["name"]: row for row in
                           [_application(item) for item in template.applications]}
            source_tasks = {row["name"]: row for row in
                            [_task(item) for item in template.tasks]}
            apps = {row["name"]: row for row in settings["applications"]}
            tasks = {row["name"]: row for row in settings["tasks"]}
            changed = {"applications": False, "tasks": False, "compose": False}
            changes_apps, changes_tasks = [], []
            files = {}
            for item in selected:
                if item.startswith("application:"):
                    name = item[len("application:"):]
                    row = source_apps[name]
                    apps[name] = row
                    baseline_apps[name] = row
                    changes_apps.append(row)
                    changed["applications"] = True
                elif item.startswith("task:"):
                    name = item[len("task:"):]
                    row = source_tasks[name]
                    tasks[name] = row
                    baseline_tasks[name] = row
                    changes_tasks.append(row)
                    changed["tasks"] = True
                elif item == "compose.yaml":
                    data = template.managed_files["compose.yaml"]
                    self._compose_check(identifier, data)
                    files["managed/compose.yaml"] = data
                    files["baseline/compose.yaml"] = data
                    changed["compose"] = True
                else:
                    raise SourceReviewInvalid("Invalid change identity")
            if changed["applications"]:
                files["managed/project.toml"] = self._render("applications", apps)
                files["baseline/project.toml"] = self._render(
                    "applications", baseline_apps,
                )
            if changed["tasks"]:
                files["managed/tasks.toml"] = self._render("tasks", tasks)
                files["baseline/tasks.toml"] = self._render("tasks", baseline_tasks)
            # Re-read the complete review just before committing SQLite.
            # Concurrent edits to the source checkout or managed definition
            # cannot silently change the approved operation.
            if self.review(identifier)["revision"] != revision:
                raise SourceSyncConflict("Source or managed configuration changed during apply")
            payload = json.dumps({"files": {
                name: base64.b64encode(data).decode("ascii")
                for name, data in files.items()
            }}, separators=(",", ":"), sort_keys=True)
            # Commit new SQL definitions and the frozen publication journal
            # atomically. Files are replayable from this durable intent record.
            with self.registry.database.transaction(write=True) as con:
                con.execute(
                    "INSERT INTO source_sync_pending(project_id,payload,started_ns) "
                    "VALUES(?,?,?)", (identifier, payload, time.time_ns()),
                )
                for row in changes_apps:
                    con.execute(
                        "DELETE FROM applications WHERE project_id=? AND name=?",
                        (identifier, row["name"]),
                    )
                    con.execute(
                        "INSERT INTO applications(project_id,name,service,port,url) "
                        "VALUES(?,?,?,?,?)",
                        (identifier, row["name"], row.get("service", ""),
                         row.get("port"), row.get("url")),
                    )
                for row in changes_tasks:
                    con.execute(
                        "DELETE FROM tasks WHERE project_id=? AND name=?",
                        (identifier, row["name"]),
                    )
                    con.execute(
                        "INSERT INTO tasks(project_id,name,service,mode,image,network,argv_json) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (identifier, row["name"], row["service"], row["mode"],
                         row["image"], row["network"],
                         json.dumps(row["command"], separators=(",", ":"))),
                    )
            self._publish(identifier, payload)
            with self.registry.database.transaction(write=True) as con:
                con.execute(
                    "DELETE FROM source_sync_pending WHERE project_id=?", (identifier,),
                )
            return {
                "project_id": identifier,
                "applied": selected,
                "review": self.review(identifier),
            }

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
