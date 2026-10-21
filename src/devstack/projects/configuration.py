"""Project-specific managed configuration, independent of Git checkouts.

Only controller users may access filesystem paths and SQL. Caller must hold
the global coordinator transition lock for mutations, to rule out racing a
configuration write with a Docker activation or a saved-task launch.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import tempfile

from devstack.domain.models import project_id
from devstack.projects.manifest import _apps, _tasks, ManifestError
from devstack.storage.projects import ProjectRegistry

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_COMPOSE = 48 * 1024


class ConfigurationConflict(RuntimeError):
    pass


class ConfigurationInvalid(ValueError):
    pass


class ConfigurationService:
    def __init__(self, registry: ProjectRegistry, workspaces, provisioner, coordinator):
        self.registry = registry
        self.workspaces = workspaces
        self.provisioner = provisioner
        self.coordinator = coordinator

    def _managed(self, identifier: str) -> Path:
        project_id(identifier)
        self.registry.get(identifier)
        folder = self.workspaces.path(identifier) / "managed"
        if folder.is_symlink() or not folder.is_dir():
            raise ConfigurationInvalid("Managed project directory unavailable")
        return folder

    def _editable(self, identifier: str) -> None:
        state = self.coordinator.status()
        if state["phase"] != "inactive" or state["project_id"] is not None:
            raise ConfigurationConflict(
                "Deactivate the runtime before editing managed project configuration"
            )
        # Never accept an uncertain Docker state as permission to reconfigure.
        self.coordinator._verify_clear()

    @staticmethod
    def _revision(blob: bytes | None) -> str:
        return hashlib.sha256(blob).hexdigest() if blob is not None else "none"

    def compose(self, identifier: str) -> dict:
        file = self._managed(identifier) / "compose.yaml"
        if file.is_symlink():
            raise ConfigurationInvalid("Symlinked Compose file")
        if not file.exists():
            return {"content": "", "revision": "none"}
        if not file.is_file() or file.stat().st_size > _MAX_COMPOSE:
            raise ConfigurationInvalid("Invalid Compose file")
        blob = file.read_bytes()
        try:
            content = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigurationInvalid("Compose content must be UTF-8") from exc
        return {"content": content, "revision": self._revision(blob)}

    def save_compose(self, identifier: str, content: str, revision: str) -> dict:
        project_id(identifier)
        if (not isinstance(content, str) or not content or
                len(content.encode("utf-8")) > _MAX_COMPOSE or "\x00" in content):
            raise ConfigurationInvalid("Compose content must be 1-48 KiB UTF-8")
        if not isinstance(revision, str) or (
            revision != "none" and not _SHA.fullmatch(revision)
        ):
            raise ConfigurationInvalid("Invalid expected Compose revision")
        with self.coordinator.locked():
            self._editable(identifier)
            folder = self._managed(identifier)
            target = folder / "compose.yaml"
            present = target.read_bytes() if target.exists() and not target.is_symlink() else None
            if target.is_symlink():
                raise ConfigurationInvalid("Symlinked Compose file")
            if self._revision(present) != revision:
                raise ConfigurationConflict("Compose content changed since it was loaded")
            # Compose schema/config validation against the trusted project source
            # directory, before publishing any file.
            source = folder
            try:
                source = self.provisioner.source_directory(identifier)
            except LookupError:
                pass
            from devstack.control.docker import _execute, compose_id
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=folder, prefix=".compose-preview-",
                delete=False,
            ) as stream:
                candidate = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                candidate.chmod(0o600)
                _execute([
                    "docker", "compose", "-p", compose_id(identifier),
                    "--project-directory", str(source), "-f", str(candidate),
                    "config", "--quiet",
                ], 50)
                os.replace(candidate, target)
            finally:
                candidate.unlink(missing_ok=True)
            return self.compose(identifier)

    def settings(self, identifier: str) -> dict:
        self.registry.get(identifier)
        with self.registry.database.transaction() as con:
            apps = con.execute(
                "SELECT name,service,port FROM applications WHERE project_id=? "
                "ORDER BY name", (identifier,),
            ).fetchall()
            tasks = con.execute(
                "SELECT name,service,mode,image,network,argv_json FROM tasks "
                "WHERE project_id=? ORDER BY name", (identifier,),
            ).fetchall()
        return {
            "applications": [dict(a) for a in apps],
            "tasks": [{
                "name": t["name"], "service": t["service"],
                "mode": t["mode"], "image": t["image"],
                "network": t["network"], "command": json.loads(t["argv_json"]),
            } for t in tasks],
        }

    def settings_review(self, identifier: str) -> dict:
        """Canonical complete collection and opaque revision for safe replacement.

        A frontend must never treat a settings collection as independent row
        updates. Revision comparison is repeated while holding the transition
        lock immediately before the write transaction.
        """
        current = self.settings(identifier)
        blob = json.dumps(current, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("utf-8")
        return {**current, "revision": self._revision(blob)}

    def replace_settings(self, identifier: str, applications: list, tasks: list,
                         expected_revision: str | None = None) -> dict:
        # Reuse exactly the parser's limits and value constraints for project
        # definitions. Never trust client-provided relative paths or raw argv.
        if not isinstance(applications, list) or not isinstance(tasks, list):
            raise ConfigurationInvalid("Expected application and task lists")
        if len(applications) > 30 or len(tasks) > 40:
            raise ConfigurationInvalid("Too many project definitions")
        # Use the same spec validator as the GitHub importer. Validate via
        # our canonical allowlist of fields, not SQL insertion exceptions.
        for row in applications:
            if not isinstance(row, dict) or set(row) - {"name", "service", "port"}:
                raise ConfigurationInvalid("Invalid application properties")
        for row in tasks:
            if not isinstance(row, dict) or set(row) - {
                "name", "service", "mode", "image", "network", "command"
            }:
                raise ConfigurationInvalid("Invalid task properties")
        # Use the importer TOML validator to keep the same strict constraints.
        def q(value):
            if not isinstance(value, str):
                raise ConfigurationInvalid("Invalid setting value")
            return json.dumps(value, ensure_ascii=True)
        app_doc = []
        for app in applications:
            app_doc.append("[[applications]]")
            for key in ("name", "service"):
                app_doc.append(f"{key} = {q(app.get(key))}")
            if "port" in app:
                value = app["port"]
                if type(value) is not int or not 1 <= value <= 65535:
                    raise ConfigurationInvalid("Invalid application port")
                app_doc.append(f"port = {value}")
        task_doc = []
        for task in tasks:
            task_doc.append("[[tasks]]")
            for key in ("name", "service", "mode", "image", "network"):
                if key in task:
                    task_doc.append(f"{key} = {q(task[key])}")
            command = task.get("command")
            if not isinstance(command, list):
                raise ConfigurationInvalid("Task command must be an array")
            task_doc.append("command = [" + ",".join(q(arg) for arg in command) + "]")
        from devstack.projects.manifest import _apps, _tasks
        try:
            validated_apps = _apps(("\n".join(app_doc) + "\n").encode("utf-8")
                                   if app_doc else b"applications=[]")
            validated_tasks = _tasks(("\n".join(task_doc) + "\n").encode("utf-8")
                                     if task_doc else b"tasks=[]")
        except (ManifestError, TypeError, ValueError) as exc:
            raise ConfigurationInvalid("Invalid project configuration") from exc
        with self.coordinator.locked():
            self._editable(identifier)
            self.registry.get(identifier)
            if expected_revision is not None:
                if not isinstance(expected_revision, str) or not _SHA.fullmatch(expected_revision):
                    raise ConfigurationInvalid("Invalid expected settings revision")
                current = self.settings_review(identifier)
                if current["revision"] != expected_revision:
                    raise ConfigurationConflict("Project definitions changed since review")
            with self.registry.database.transaction(write=True) as con:
                con.execute("DELETE FROM applications WHERE project_id=?", (identifier,))
                con.execute("DELETE FROM tasks WHERE project_id=?", (identifier,))
                for app in validated_apps:
                    con.execute(
                        "INSERT INTO applications(project_id,name,service,port) "
                        "VALUES(?,?,?,?)", (identifier, app.name, app.service, app.port)
                    )
                for task in validated_tasks:
                    con.execute(
                        "INSERT INTO tasks(project_id,name,service,mode,image,network,argv_json)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (identifier, task.name, task.service, task.mode,
                         task.image, task.network, json.dumps(task.argv)),
                    )
        return self.settings(identifier)
