"""Controller-only Docker Compose adapter.

Every managed Compose project uses its immutable project UUID, never a display
name or the source template's 'name' field. Label checks are authoritative;
Docker CLI success alone does not establish cleanup.
"""
from pathlib import Path
import json
import selectors
import time
import os
import re
import subprocess

from devstack.domain.models import project_id
from devstack.projects.service import ProjectProvisioner
from devstack.projects.workspaces import WorkspaceStore

_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_CONTAINER_ID = re.compile(r"[a-f0-9]{64}\Z")
_LOG_TAIL_MAX = 200
_LOG_OUTPUT_LIMIT = 64 * 1024


def _tail_logs(container_id: str, tail: int) -> dict:
    """Bound both Docker log output memory and execution time.

    Keep recent bytes only; a single enormous log line cannot allocate
    unlimited memory in the privileged controller.
    """
    if not _CONTAINER_ID.fullmatch(container_id):
        raise DockerControlError("Invalid container identity")
    if type(tail) is not int or not 1 <= tail <= _LOG_TAIL_MAX:
        raise DockerControlError("Invalid log tail count")
    try:
        process = subprocess.Popen(
            ["docker", "logs", "--timestamps", "--tail", str(tail), "--", container_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        raise DockerControlError("Cannot start Docker log reader") from exc
    output = bytearray()
    truncated = False
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 12
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DockerControlError("Docker log read timed out")
            events = selector.select(remaining)
            if not events:
                raise DockerControlError("Docker log read timed out")
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > _LOG_OUTPUT_LIMIT:
                truncated = True
                del output[:-_LOG_OUTPUT_LIMIT]
        if process.wait(timeout=2) != 0:
            raise DockerControlError("Docker log read failed")
        return {"text": output.decode("utf-8", errors="replace"),
                "truncated": truncated, "tail": tail}
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerControlError("Docker log read failed") from exc
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()




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
            candidate = suffix if compose.startswith("devstack-") else extra
            try:
                project_id(candidate)
                valid = (compose == compose_id(candidate) or
                         (not compose and extra == candidate))
            except ValueError:
                valid = False
            if extra and extra != candidate:
                valid = False
            found.append({
                "id": ident, "project": candidate if valid else "",
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

    def task_command(self, identifier: str, task: dict,
                     generation: str, job_id: str) -> list[str]:
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
            from uuid import UUID
            try:
                UUID(job_id)
            except ValueError as exc:
                raise DockerControlError("Invalid job ID") from exc
            if not re.fullmatch(r"[0-9a-f]{32}", generation):
                raise DockerControlError("Invalid activation generation")
            return [
                "docker", "run", "--rm", "--name", "devstack-task-" + job_id,
                "--label", "devstack.project_id=" + identifier,
                "--label", "devstack.generation=" + generation,
                "--label", "devstack.job_id=" + job_id,
                "--network", network,
                "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
                image, *argv,
            ]
        service = task["service"]
        if not _SERVICE.fullmatch(service):
            raise DockerControlError("Invalid saved task service")
        mode = task["mode"]
        if mode == "exec":
            return self.command(identifier) + ["exec", "-T", service, *argv]
        if mode == "run":
            from uuid import UUID
            try:
                UUID(job_id)
            except ValueError as exc:
                raise DockerControlError("Invalid job UUID") from exc
            if not re.fullmatch(r"[0-9a-f]{32}", generation):
                raise DockerControlError("Invalid project generation")
            return self.command(identifier) + [
                "run", "--rm", "--no-deps", "-T",
                "--name", "devstack-task-" + job_id,
                "--label", "devstack.project_id=" + identifier,
                "--label", "devstack.generation=" + generation,
                "--label", "devstack.job_id=" + job_id,
                service, *argv,
            ]
        raise DockerControlError("Invalid saved task mode")

    def stop_job(self, identifier: str, generation: str, job_id: str) -> None:
        """Stop only a job-owned disposable container; never stop a service.

        The Docker CLI process may disappear before the container stops.
        Inspect labels and verify removal of the specifically owned container.
        """
        from uuid import UUID
        project_id(identifier)
        try:
            UUID(job_id)
        except ValueError as exc:
            raise DockerControlError("Invalid job UUID") from exc
        if not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise DockerControlError("Invalid project generation")
        name = "devstack-task-" + job_id

        def inspect():
            try:
                result = subprocess.run(
                    ["docker", "inspect", "--type", "container", "--", name],
                    capture_output=True, text=True, timeout=20, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DockerControlError("Cannot inspect job container") from exc
            if result.returncode != 0:
                # Distinguish a missing container from Docker daemon errors.
                error = result.stderr.lower()
                if "no such object" in error or "no such container" in error:
                    return None
                raise DockerControlError("Docker job inspection failed")
            try:
                records = json.loads(result.stdout)
                if len(records) != 1:
                    raise ValueError("ambiguous")
                return records[0]
            except (ValueError, TypeError, KeyError) as exc:
                raise DockerControlError("Malformed job container inspection") from exc

        record = inspect()
        if record is None:
            return
        labels = record.get("Config", {}).get("Labels") or {}
        if (record.get("Name") != "/" + name
                or labels.get("devstack.project_id") != identifier
                or labels.get("devstack.generation") != generation
                or labels.get("devstack.job_id") != job_id):
            raise DockerControlError("Refusing to stop a container not owned by this job")
        container_id = record.get("Id")
        if not isinstance(container_id, str) or not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise DockerControlError("Cannot verify Docker container identity")
        _execute(["docker", "rm", "-f", "--", container_id], 30)
        if inspect() is not None:
            raise DockerControlError("Job container termination not verified")

    @staticmethod
    def _checked_service_container(identifier: str, generation: str,
                                   container_id: str, *, allow_oneoff: bool = False) -> dict:
        """Inspect Docker identity and reject other projects/jobs/generations."""
        project_id(identifier)
        if not re.fullmatch(r"[a-f0-9]{32}", generation):
            raise DockerControlError("Invalid activation generation")
        if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
            raise DockerControlError("Invalid container identity")
        data = json.loads(_execute(
            ["docker", "inspect", "--type", "container", "--", container_id], 20,
        ))
        if not isinstance(data, list) or len(data) != 1:
            raise DockerControlError("Ambiguous Docker container inspection")
        record = data[0]
        if record.get("Id") != container_id:
            raise DockerControlError("Docker identity changed during inspection")
        labels = record.get("Config", {}).get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        if (labels.get("com.docker.compose.project") != compose_id(identifier)
                or labels.get("devstack.project_id") != identifier
                or labels.get("devstack.generation") != generation
                or not isinstance(service, str) or not _SERVICE.fullmatch(service)
                or (not allow_oneoff and
                    str(labels.get("com.docker.compose.oneoff", "")).lower() in ("true", "1"))
                or (not allow_oneoff and labels.get("devstack.job_id"))):
            raise DockerControlError("Container is not an owned Compose service")
        return record

    def service_snapshot(self, identifier: str, generation: str) -> dict:
        """Configured Compose services and their verified owned containers."""
        project_id(identifier)
        if not re.fullmatch(r"[a-f0-9]{32}", generation):
            raise DockerControlError("Invalid activation generation")
        services = _execute(self.command(identifier) + [
            "config", "--services",
        ], 25).splitlines()
        if (len(services) > 96 or not services or
                any(not _SERVICE.fullmatch(name) for name in services) or
                len(set(services)) != len(services)):
            raise DockerControlError("Invalid Compose service configuration")
        ids = _execute([
            "docker", "ps", "--all", "--no-trunc", "--quiet",
            "--filter", "label=com.docker.compose.project=" + compose_id(identifier),
        ], 25).splitlines()
        if len(ids) > 64 or len(set(ids)) != len(ids):
            raise DockerControlError("Ambiguous or excessive Docker containers")
        rows = {name: [] for name in services}
        for container_id in ids:
            if not _CONTAINER_ID.fullmatch(container_id):
                raise DockerControlError("Invalid Docker container ID")
            record = self._checked_service_container(
                identifier, generation, container_id,
                allow_oneoff=True,
            )
            labels = record["Config"]["Labels"]
            # Compose run containers are jobs, not ordinary Compose services.
            if str(labels.get("com.docker.compose.oneoff", "")).lower() in ("true", "1"):
                continue
            service = labels["com.docker.compose.service"]
            if service not in rows:
                raise DockerControlError("Unexpected Compose service in project")
            state = record.get("State", {})
            health = state.get("Health")
            rows[service].append({
                "id": container_id, "name": str(record.get("Name", "")).lstrip("/")[:180],
                "state": str(state.get("Status", "unknown"))[:40],
                "health": (str(health.get("Status", "unknown"))[:40]
                           if isinstance(health, dict) else None),
                "restart_count": record.get("RestartCount", 0)
                                 if type(record.get("RestartCount", 0)) is int else 0,
            })
        return {"services": [
            {"name": name, "containers": sorted(rows[name], key=lambda x: x["name"]),
             "status": ("not-created" if not rows[name] else
                        ("running" if any(c["state"] == "running" for c in rows[name])
                         else rows[name][0]["state"]))}
            for name in services
        ]}

    def service_logs(self, identifier: str, generation: str,
                     container_id: str, tail: int) -> dict:
        record = self._checked_service_container(identifier, generation, container_id)
        result = _tail_logs(container_id, tail)
        return {"container_id": container_id,
                "service": record["Config"]["Labels"]["com.docker.compose.service"],
                **result}

    def private_published_ports(self, identifier: str, generation: str,
                                host: str) -> list[dict]:
        """Read exact Tailscale-bound TCP mappings of running Compose services.

        Docker is authoritative. Wildcard, loopback, public/non-tailnet IPs
        and disposable one-off containers never produce private URLs.
        """
        import ipaddress
        project_id(identifier)
        try:
            address = ipaddress.ip_address(host)
            if (address.version != 4 or
                    address not in ipaddress.ip_network("100.64.0.0/10")):
                raise ValueError("Not a Tailscale IPv4 address")
        except (ValueError, TypeError) as exc:
            raise DockerControlError("Invalid private Tailscale binding address") from exc

        snapshot = self.service_snapshot(identifier, generation)
        published = set()
        for service in snapshot["services"]:
            for item in service["containers"]:
                if item["state"] != "running":
                    continue
                record = self._checked_service_container(
                    identifier, generation, item["id"],
                )
                if (record["Config"]["Labels"]["com.docker.compose.service"]
                        != service["name"]):
                    raise DockerControlError("Inconsistent Compose service ownership")
                if not record.get("State", {}).get("Running"):
                    continue
                mappings = record.get("NetworkSettings", {}).get("Ports") or {}
                if not isinstance(mappings, dict):
                    raise DockerControlError("Invalid Docker port metadata")
                for target, hosts in mappings.items():
                    if not isinstance(target, str) or not target.endswith("/tcp"):
                        continue
                    try:
                        container_port = int(target[:-4])
                    except ValueError:
                        continue
                    if not 1 <= container_port <= 65535:
                        continue
                    if not isinstance(hosts, list):
                        continue
                    for mapping in hosts:
                        if not isinstance(mapping, dict):
                            continue
                        if mapping.get("HostIp") != host:
                            continue
                        try:
                            host_port = int(mapping.get("HostPort"))
                        except (ValueError, TypeError):
                            continue
                        if not 1 <= host_port <= 65535:
                            continue
                        published.add((
                            service["name"], container_port, host_port,
                        ))
        return [
            {"service": service, "container_port": container_port,
             "host_port": host_port, "url": f"http://{host}:{host_port}/"}
            for service, container_port, host_port in sorted(published)
        ]

    def webhook_target(self, identifier: str, generation: str,
                       service: str) -> str:
        """Return verified private IPv4 of exactly one active Compose service.

        Never accept client-supplied addresses: the target must be obtained
        from Docker inspect for the current immutable project/generation.
        """
        import ipaddress
        project_id(identifier)
        if not _SERVICE.fullmatch(service):
            raise DockerControlError("Invalid webhook service")
        if not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise DockerControlError("Invalid activation generation")
        output = _execute(self.command(identifier) + [
            "ps", "-q", service,
        ], 35).splitlines()
        if len(output) != 1:
            raise DockerControlError("Webhook requires exactly one running Compose service")
        data = json.loads(_execute(["docker", "inspect", "--", output[0]], 35))
        if len(data) != 1:
            raise DockerControlError("Docker inspection returned ambiguous service")
        record = data[0]
        labels = record.get("Config", {}).get("Labels", {})
        if (labels.get("com.docker.compose.project") != compose_id(identifier)
                or labels.get("com.docker.compose.service") != service
                or labels.get("devstack.project_id") != identifier
                or labels.get("devstack.generation") != generation
                or not record.get("State", {}).get("Running")):
            raise DockerControlError("Webhook service ownership could not be verified")
        networks = record.get("NetworkSettings", {}).get("Networks", {})
        matches = []
        for net_name, details in networks.items():
            labels = json.loads(_execute([
                "docker", "network", "inspect", net_name,
                "--format", "{{json .Labels}}",
            ], 35))
            if (labels.get("com.docker.compose.project") == compose_id(identifier)
                    and labels.get("com.docker.compose.network") == "default"):
                host = details.get("IPAddress", "")
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    continue
                if address.version == 4 and address.is_private and not address.is_loopback:
                    matches.append(host)
        if len(matches) != 1:
            raise DockerControlError("No unambiguous private default project network target")
        return matches[0]
