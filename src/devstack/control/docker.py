"""Controller-only Docker Compose adapter.

Every managed Compose project uses its immutable project UUID, never a display
name or the source template's 'name' field. Label checks are authoritative;
Docker CLI success alone does not establish cleanup.
"""
from pathlib import Path
import json
import os
import re
import subprocess

from devstack.domain.models import project_id
from devstack.projects.service import ProjectProvisioner
from devstack.projects.workspaces import WorkspaceStore

_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


class DockerControlError(RuntimeError):
    pass


def compose_id(identifier: str) -> str:
    project_id(identifier)
    return "devstack-" + identifier


def _execute(argv: list[str], timeout: int = 240) -> str:
    try:
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerControlError("Docker operation failed or exceeded its time limit") from exc
    if result.returncode:
        raise DockerControlError(
            "Docker command failed: " + result.stderr[-600:].strip()
        )
    return result.stdout.strip()


class DockerBackend:
    """Only a trusted controller Linux user with Docker access may instantiate."""

    def __init__(self, provisioner: ProjectProvisioner, workspaces: WorkspaceStore):
        self.provisioner = provisioner
        self.workspaces = workspaces

    def _base(self, identifier: str) -> tuple[list[str], Path]:
        root = self.workspaces.path(identifier)
        compose = root / "managed" / "compose.yaml"
        if compose.is_symlink() or not compose.is_file():
            raise DockerControlError("No managed Compose definition for project")
        try:
            source = self.provisioner.source_directory(identifier)
        except LookupError:
            source = root / "managed"
        return ([
            "docker", "compose", "-p", compose_id(identifier),
            "--project-directory", str(source), "-f", str(compose),
        ], root)

    def validate(self, identifier: str) -> None:
        argv, _ = self._base(identifier)
        _execute(argv + ["config", "--quiet"], 40)
        services = _execute(argv + ["config", "--services"], 40).splitlines()
        if not services or any(not _SERVICE.fullmatch(x) for x in services):
            raise DockerControlError("Compose must define valid services")

    def _overlay(self, identifier: str, generation: str) -> Path:
        argv, root = self._base(identifier)
        services = _execute(argv + ["config", "--services"], 40).splitlines()
        if not services or any(not _SERVICE.fullmatch(x) for x in services):
            raise DockerControlError("Invalid Compose services")
        # The override lives beside the managed Compose file. The explicit
        # project-directory remains the original GitHub manifest for relative
        # build contexts, so imports keep resolving ../app as intended.
        overlay = root / "managed" / ".runtime-activation.yaml"
        if overlay.is_symlink():
            raise DockerControlError("Refusing symlinked runtime overlay")
        parts = ["services:"]
        for name in services:
            parts.extend([
                "  " + name + ":",
                "    labels:",
                "      devstack.project_id: " + json.dumps(identifier),
                "      devstack.generation: " + json.dumps(generation),
            ])
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=overlay.parent,
            prefix=".runtime-", delete=False,
        ) as out:
            temp = Path(out.name)
            out.write("\n".join(parts) + "\n")
            out.flush()
            os.fsync(out.fileno())
        try:
            temp.chmod(0o600)
            os.replace(temp, overlay)
        finally:
            temp.unlink(missing_ok=True)
        return overlay

    def command(self, identifier: str) -> list[str]:
        argv, root = self._base(identifier)
        overlay = root / "managed" / ".runtime-activation.yaml"
        if not overlay.is_file() or overlay.is_symlink():
            raise DockerControlError("No active Compose generation overlay")
        return argv + ["-f", str(overlay)]

    def start(self, identifier: str, generation: str) -> None:
        self.validate(identifier)
        overlay = self._overlay(identifier, generation)
        argv, _ = self._base(identifier)
        _execute(argv + ["-f", str(overlay), "up", "-d", "--build",
                         "--wait", "--wait-timeout", "150"], 240)

    def stop(self, identifier: str) -> None:
        # A missing/malformed YAML cannot justify abandoning managed Docker
        # containers. Fall back to exact, verified ownership-based removals.
        try:
            argv, _ = self._base(identifier)
            _execute(argv + ["down", "--remove-orphans"], 120)
        except (DockerControlError, OSError):
            pass
        remaining = self.owned()
        ids = [item["id"] for item in remaining if item["project"] == identifier]
        if ids:
            _execute(["docker", "rm", "-f", "--", *ids], 90)
        if any(x["project"] == identifier for x in self.owned()):
            raise DockerControlError("Docker cleanup could not be verified")

    def owned(self) -> list[dict[str, str]]:
        """Enumerate every DevStack-owned container, including stopped ones.

        Unknown/malformed devstack projects are returned with project="" so
        the coordinator will refuse new activation rather than ignore them.
        """
        output = _execute(
            ["docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}"], 35,
        )
        found = []
        for line in output.splitlines():
            try:
                record = json.loads(line)
                ident = record["ID"]
                labels = dict(
                    pair.split("=", 1) for pair in
                    record.get("Labels", "").split(",")
                    if "=" in pair
                )
            except (ValueError, TypeError, KeyError) as exc:
                raise DockerControlError("Unverifiable Docker container metadata") from exc
            compose = labels.get("com.docker.compose.project", "")
            extra = labels.get("devstack.project_id", "")
            if not compose.startswith("devstack-") and not extra:
                continue
            suffix = compose.removeprefix("devstack-")
            try:
                project_id(suffix)
                valid = compose == compose_id(suffix)
            except ValueError:
                valid = False
            if extra and extra != suffix:
                valid = False
            found.append({
                "id": ident, "project": suffix if valid else "",
                "generation": labels.get("devstack.generation", ""),
                "state": record.get("State", ""),
            })
        return found

    def verify(self, identifier: str, generation: str) -> None:
        owned = self.owned()
        if any(not item["project"] for item in owned):
            raise DockerControlError("Unknown DevStack-owned Docker container")
        items = [item for item in owned if item["project"] == identifier]
        if not items or not any(x["state"] == "running" for x in items):
            raise DockerControlError("No running project containers after activation")
        if any(x["generation"] != generation for x in items):
            raise DockerControlError("Project container generation mismatch")

    def task_command(self, identifier: str, task: dict) -> list[str]:
        """Translate a registered task to a fixed Docker argv without a host shell."""
        argv = task["argv"]
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(arg, str) or not arg for arg in argv)):
            raise DockerControlError("Invalid saved task command")
        image = task["image"]
        if image:
            if task["mode"] != "run":
                raise DockerControlError("Standalone tasks must use run")
            state_network = task["network"]
            if state_network == "none":
                network = "none"
            elif state_network == "project":
                output = _execute([
                    "docker", "network", "ls",
                    "--filter", "label=com.docker.compose.project=" + compose_id(identifier),
                    "--filter", "label=com.docker.compose.network=default",
                    "--format", "{{.ID}}",
                ], 30).splitlines()
                if len(output) != 1:
                    raise DockerControlError("Project default network is unavailable")
                network = output[0]
            else:
                raise DockerControlError("Invalid task network")
            # Job IDs and generation are not accepted from the caller here.
            # The next stage will ensure every disposable image task carries
            # an exact controller-generated name and activation-generation label.
            raise DockerControlError(
                "Standalone image execution requires generation-scoped launch metadata"
            )
        service = task["service"]
        if not _SERVICE.fullmatch(service):
            raise DockerControlError("Invalid saved task service")
        mode = task["mode"]
        if mode == "exec":
            return self.command(identifier) + ["exec", "-T", service, *argv]
        if mode == "run":
            return self.command(identifier) + ["run", "--rm", "--no-deps", "-T",
                                               service, *argv]
        raise DockerControlError("Invalid saved task mode")
