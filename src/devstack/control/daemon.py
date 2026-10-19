"""Narrow local Unix-socket controller API.

This listener is owned by the dedicated Docker-privileged Linux identity.
The browser/API identity may access only the explicitly allowlisted operations
through a group-restricted socket; it never obtains a Docker socket or shell.
"""
import argparse
from contextlib import contextmanager
import fcntl
import grp
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import threading

from devstack.projects.service import ProjectProvisioner
from devstack.projects.configuration import ConfigurationService
from devstack.projects.repositories import RepositoryGitService
from devstack.projects.source_sync import SourceReviewService
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database
from devstack.storage.projects import ProjectRegistry
from .coordinator import Coordinator
from .docker import DockerBackend
from .jobs import JobSupervisor
from .observability import ContainerObservability
from .imports import ImportSupervisor
from .repository_add import RepositoryAddSupervisor
from devstack.github_ssh import public_key, generate_key, test_connection
from .ingress import WebhookIngress, WebhookServer

_MAX_MESSAGE = 64 * 1024


class ProtocolError(ValueError):
    pass


class Controller:
    def __init__(self, database: Database, workspaces: WorkspaceStore, backend=None):
        self.registry = ProjectRegistry(database)
        self.provisioner = ProjectProvisioner(self.registry, workspaces)
        self.backend = backend or DockerBackend(self.provisioner, workspaces)
        self.coordinator = Coordinator(database, self.registry, self.backend)
        self.jobs = JobSupervisor(self.coordinator, self.backend, workspaces)
        self.observability = ContainerObservability(self.coordinator, self.backend)
        self.imports = ImportSupervisor(database, self.registry, self.provisioner)
        self.configuration = ConfigurationService(self.registry, workspaces, self.provisioner, self.coordinator)
        self.repositories = RepositoryGitService(self.registry, workspaces, self.coordinator)
        self.repository_add = RepositoryAddSupervisor(database, self.registry, workspaces, self.coordinator)
        self.source_review = SourceReviewService(self.registry, workspaces, self.provisioner, self.configuration)
        self.ingress = WebhookIngress(self.coordinator, self.registry, self.backend)
        self.coordinator.ingress = self.ingress

    def handle(self, request: dict) -> dict:
        if not isinstance(request, dict) or not isinstance(request.get("op"), str):
            raise ProtocolError("Expected a controller operation")
        operation = request["op"]
        fields = {
            "health": {"op"},
            "status": {"op"},
            "projects": {"op"},
            "project_detail": {"op", "project_id"},
            "project_rename": {"op", "project_id", "name"},
            "source_review": {"op", "project_id"},
            "source_apply": {"op", "project_id", "revision", "selected", "overwrites"},
            "repository_status": {"op", "project_id", "repository_id"},
            "repository_update": {"op", "project_id", "repository_id", "expected_head"},
            "repository_add_submit": {"op", "project_id", "url", "name", "branch", "request_key"},
            "repository_add_status": {"op", "project_id", "id"},
            "github_ssh_status": {"op"},
            "github_ssh_generate": {"op"},
            "github_ssh_test": {"op"},
            "compose_read": {"op", "project_id"},
            "compose_save": {"op", "project_id", "content", "revision"},
            "settings_read": {"op", "project_id"},
            "settings_replace": {"op", "project_id", "applications", "tasks"},
            "webhooks": {"op", "project_id"},
            "webhook_upsert": {"op", "project_id", "route", "service", "port", "destination"},
            "webhook_remove": {"op", "project_id", "route"},
            "project_create": {"op", "slug", "name"},
            "import_submit": {"op", "url", "slug", "name", "request_key"},
            "import_status": {"op", "id"},
            "activate": {"op", "project_id"},
            "switch": {"op", "project_id"},
            "deactivate": {"op"},
            "reconcile": {"op"},
            "task": {"op", "project_id", "name"},
            "compose": {"op", "project_id", "action"},
            "job": {"op", "project_id", "id"},
            "adhoc": {"op", "project_id", "kind", "service", "image", "network", "command"},
            "jobs": {"op", "project_id"},
            "services": {"op", "project_id"},
            "service_logs": {"op", "project_id", "container_id", "tail"},
            "job_logs": {"op", "project_id", "id", "offset"},
            "job_cancel": {"op", "project_id", "id"},
        }
        if operation not in fields or set(request) != fields[operation]:
            raise ProtocolError("Unexpected or missing controller parameters")
        # A committed but incompletely published sync is a global barrier.
        # The operator can explicitly request reconciliation/replay.
        if operation != "reconcile":
            with self.registry.database.transaction() as con:
                if con.execute("SELECT 1 FROM source_sync_pending LIMIT 1").fetchone():
                    from devstack.projects.source_sync import SourceSyncConflict
                    raise SourceSyncConflict("Source sync publication is pending recovery")
        if operation == "health":
            state = self.coordinator.status()
            # A successful Unix-socket ping alone is not sufficient to
            # certify a v2 deployment: registry must open and an inactive
            # supervisor must have no residual owned workloads.
            self.registry.list()
            if state["phase"] == "inactive":
                self.coordinator._verify_clear()
            return {"alive": True, "runtime": state["phase"]}
        if operation == "status":
            return self._public_status(self.coordinator.status())
        if operation == "projects":
            return {"projects": [
                {"id": p.id, "slug": p.slug, "name": p.name}
                for p in self.registry.list()
            ]}
        if operation == "project_detail":
            identifier = request["project_id"]
            entry = self.registry.get(identifier)
            repositories = self.registry.repositories(identifier)
            with self.registry.database.transaction() as con:
                apps = con.execute(
                    "SELECT name,service,port FROM applications WHERE project_id=? "
                    "ORDER BY name", (identifier,)
                ).fetchall()
                tasks = con.execute(
                    "SELECT name,service,mode,image,network FROM tasks WHERE project_id=? "
                    "ORDER BY name", (identifier,)
                ).fetchall()
            return {
                "project": {"id": entry.id, "slug": entry.slug, "name": entry.name},
                "repositories": [
                    {"id": item.id, "name": item.name, "relative_path": item.relative_path}
                    for item in repositories
                ],
                "linked_source": self._has_linked_source(identifier),
                "applications": [dict(row) for row in apps],
                "tasks": [dict(row) for row in tasks],
            }
        if operation == "project_rename":
            entry = self.registry.rename(request["project_id"], request["name"])
            return {"id": entry.id, "slug": entry.slug, "name": entry.name}
        if operation == "source_review":
            return self.source_review.review(request["project_id"])
        if operation == "source_apply":
            return self.source_review.apply(
                request["project_id"], request["revision"],
                request["selected"], request["overwrites"],
            )
        if operation == "repository_add_submit":
            return self.repository_add.submit(
                request["project_id"], request["url"], request["name"],
                request["branch"], request["request_key"],
            )
        if operation == "repository_add_status":
            return self.repository_add.get(request["project_id"], request["id"])
        if operation == "github_ssh_status":
            return {"public_key": public_key()}
        if operation == "github_ssh_generate":
            return {"public_key": generate_key()}
        if operation == "github_ssh_test":
            return {"authenticated_user": test_connection()}
        if operation == "repository_status":
            return self.repositories.status(request["project_id"], request["repository_id"])
        if operation == "repository_update":
            return self.repositories.update(
                request["project_id"], request["repository_id"], request["expected_head"],
            )
        if operation == "compose_read":
            return self.configuration.compose(request["project_id"])
        if operation == "compose_save":
            return self.configuration.save_compose(
                request["project_id"], request["content"], request["revision"],
            )
        if operation == "settings_read":
            return self.configuration.settings(request["project_id"])
        if operation == "settings_replace":
            return self.configuration.replace_settings(
                request["project_id"], request["applications"], request["tasks"],
            )
        if operation == "webhooks":
            return {"webhooks": self.ingress.list(request["project_id"])}
        if operation == "webhook_upsert":
            return self.ingress.configure(
                request["project_id"], request["route"], request["service"],
                request["port"], request["destination"],
            )
        if operation == "webhook_remove":
            self.ingress.remove(request["project_id"], request["route"])
            return {"removed": True}
        if operation == "project_create":
            entry = self.provisioner.create_empty(request["slug"], request["name"])
            return {"id": entry.id, "name": entry.name}
        if operation == "import_submit":
            return self.imports.submit(
                request["url"], request["slug"], request["name"],
                request["request_key"],
            )
        if operation == "import_status":
            return self.imports.get(request["id"])
        if operation == "activate":
            # Resolve explicit identity even when another project is active.
            # Unknown project IDs are never silently interpreted as the active one.
            self.registry.get(request["project_id"])
            return self._public_status(self.coordinator.activate(request["project_id"]))
        if operation == "switch":
            return self._public_status(self.coordinator.switch(request["project_id"]))
        if operation == "deactivate":
            return self._public_status(self.coordinator.deactivate())
        if operation == "reconcile":
            # Reconcile actual execution before replaying file snapshots, then
            # verify absence of both owned workloads and unfinished journals.
            self.coordinator.reconcile()
            self.source_review.recover_pending()
            self.repository_add.reconcile()
            return self._public_status(self.coordinator.status())
        if operation == "task":
            return {"job_id": self.jobs.submit_task(request["project_id"], request["name"])}
        if operation == "compose":
            return {"job_id": self.jobs.submit_compose(
                request["project_id"], request["action"])}
        if operation == "adhoc":
            return {"job_id": self.jobs.submit_adhoc(
                request["project_id"], request["kind"], request["service"],
                request["image"], request["network"], request["command"],
            )}
        if operation == "services":
            return self.observability.services(request["project_id"])
        if operation == "service_logs":
            return self.observability.logs(
                request["project_id"], request["container_id"], request["tail"],
            )
        if operation == "jobs":
            return self.jobs.list(request["project_id"])
        if operation == "job_logs":
            return self.jobs.logs(
                request["project_id"], request["id"], request["offset"],
            )
        if operation == "job_cancel":
            return self.jobs.cancel(request["project_id"], request["id"])
        if operation == "job":
            from devstack.domain.models import project_id
            project_id(request["project_id"])
            self.registry.get(request["project_id"])
            job = self.jobs.get(request["id"])
            if job["project_id"] != request["project_id"]:
                raise ProtocolError("Job does not belong to requested project")
            return job
        raise ProtocolError("Unknown operation")

    def _has_linked_source(self, identifier: str) -> bool:
        with self.registry.database.transaction() as con:
            return con.execute(
                "SELECT 1 FROM project_sources WHERE project_id=?", (identifier,),
            ).fetchone() is not None

    @staticmethod
    def _public_status(state: dict) -> dict:
        return {key: value for key, value in state.items() if key != "generation"}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(_MAX_MESSAGE + 1)
            if not line or len(line) > _MAX_MESSAGE or not line.endswith(b"\n"):
                raise ProtocolError("Invalid controller request length")
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ProtocolError("Expected a JSON object")
            result = self.server.controller.handle(data)
            response = {"ok": True, "result": result}
        except (Exception) as exc:
            # Controller interface is local/private but must not expose paths,
            # process output, command argv or environment secrets on errors.
            response = {"ok": False, "error": type(exc).__name__}
        self.wfile.write(json.dumps(response).encode("utf-8") + b"\n")


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, location: Path, controller: Controller,
                 socket_group: str | None = None):
        self.location = Path(location)
        if self.location.exists() or self.location.is_symlink():
            raise ProtocolError("Control socket path already exists")
        if any(p.is_symlink() for p in self.location.parents):
            raise ProtocolError("Control socket parent must not be symlinked")
        self.location.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        super().__init__(str(self.location), _Handler)
        self.controller = controller
        try:
            os.chmod(self.location, 0o660)
            if socket_group is not None:
                gid = grp.getgrnam(socket_group).gr_gid
                os.chown(self.location, -1, gid)
        except Exception:
            self.server_close()
            self.location.unlink(missing_ok=True)
            raise

    def server_close(self):
        super().server_close()
        self.location.unlink(missing_ok=True)


def request(location: Path, payload: dict, timeout: float = 20) -> dict:
    """Tiny client for the next project's Flask API adapter."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(location))
        connection.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        reply = bytearray()
        while not reply.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk or len(reply) + len(chunk) > _MAX_MESSAGE:
                raise ProtocolError("Invalid controller response")
            reply.extend(chunk)
    result = json.loads(reply)
    if not result.get("ok"):
        raise ProtocolError("Controller rejected operation: " + result.get("error", "unknown"))
    return result["result"]


@contextmanager
def exclusive_daemon(state: Path):
    """Ensure one controller process owns all jobs for a persistent state DB.

    Flock is held for the entire daemon lifetime; the kernel releases it on
    crashes. A different --socket path cannot start a second supervisor.
    """
    root = Path(state)
    if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise ProtocolError("Controller state path must not contain symlinks")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    location = root / ".controller-instance.lock"
    fd = os.open(location, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProtocolError("A control daemon already owns this state") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description="DevStack private runtime controller")
    parser.add_argument("--state", type=Path, default=Path("/var/lib/devstack"))
    parser.add_argument("--socket", type=Path,
                        default=Path("/run/devstack-control/control.sock"))
    parser.add_argument("--socket-group", default=None)
    parser.add_argument("--webhook-port", type=int, default=8767)
    args = parser.parse_args()
    with exclusive_daemon(args.state):
        database = Database(args.state / "control.sqlite3")
        workspaces = WorkspaceStore(args.state / "projects")
        controller = Controller(database, workspaces)
        # Exclusive daemon lifetime lock guarantees that all handlers from an
        # earlier supervisor are gone. Their leases cannot be active now.
        with controller.coordinator.locked():
            controller.ingress.clear_after_daemon_restart()
        try:
            controller.imports.reconcile()
        except Exception:
            # A partially published source remains explicit; never delete it.
            pass
        try:
            controller.coordinator.reconcile()
            controller.source_review.recover_pending()
            controller.repository_add.reconcile()
        except Exception:
            # A pending durable journal remains as a fail-closed barrier;
            # explicit reconcile retries runtime cleanup and replay.
            pass
        with ControlServer(args.socket, controller, args.socket_group) as server:
            with WebhookServer(controller.ingress, args.webhook_port) as public_ingress:
                webhook_thread = threading.Thread(
                    target=public_ingress.serve_forever,
                    name="devstack-webhook-ingress", daemon=True,
                )
                webhook_thread.start()
                def stop(_signum, _frame):
                    threading.Thread(target=server.shutdown, daemon=True).start()

                signal.signal(signal.SIGTERM, stop)
                signal.signal(signal.SIGINT, stop)
                try:
                    server.serve_forever(poll_interval=0.2)
                finally:
                    public_ingress.shutdown()
                    webhook_thread.join(timeout=10)
                    try:
                        controller.coordinator.reconcile()
                    except Exception:
                        pass


if __name__ == "__main__":
    main()
