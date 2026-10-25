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
import socketserver
import threading

from devstack.projects.service import ProjectProvisioner
from devstack.projects.configuration import ConfigurationService
from devstack.projects.repositories import RepositoryGitService
from devstack.projects.source_sync import SourceReviewService
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database
from devstack.storage.projects import ProjectRegistry
from devstack.diagnostics import DiagnosticStore
from .coordinator import Coordinator
from .docker import DockerBackend
from .jobs import JobSupervisor
from .observability import ContainerObservability
from .private_ports import PrivateApplicationPorts
from .imports import ImportSupervisor
from .repository_add import RepositoryAddSupervisor
from .transitions import TransitionSupervisor
from .bulk_repositories import BulkRepositoryUpdater
from .repository_sync import RepositorySyncSupervisor
from .reactions import ReactionSupervisor
from devstack.github_ssh import public_key, generate_key, test_connection
from devstack.ipc.client import ControllerRejected, ControllerUnavailable, send_payload
from devstack.ipc.protocol import (
    MAX_MESSAGE, ProtocolError, protocol_info, validate_request,
)
from .ingress import WebhookIngress, WebhookServer

_MAX_MESSAGE = MAX_MESSAGE


class Controller:
    def __init__(self, database: Database, workspaces: WorkspaceStore, backend=None):
        self.registry = ProjectRegistry(database)
        self.diagnostics = DiagnosticStore(database.path.parent / 'control-diagnostics.sqlite3')
        self.provisioner = ProjectProvisioner(self.registry, workspaces)
        self.backend = backend or DockerBackend(self.provisioner, workspaces)
        self.coordinator = Coordinator(database, self.registry, self.backend)
        self.transitions = TransitionSupervisor(database, self.coordinator, self.registry,
                                                self._reconcile)
        self.jobs = JobSupervisor(self.coordinator, self.backend, workspaces)
        self.observability = ContainerObservability(self.coordinator, self.backend)
        self.private_ports = PrivateApplicationPorts(self.coordinator, self.backend)
        self.imports = ImportSupervisor(database, self.registry, self.provisioner)
        self.configuration = ConfigurationService(self.registry, workspaces, self.provisioner, self.coordinator)
        self.repositories = RepositoryGitService(self.registry, workspaces, self.coordinator)
        self.repository_sync = RepositorySyncSupervisor(
            database, self.registry, self.coordinator, self.repositories,
        )
        self.reactions = ReactionSupervisor(
            database, self.registry, self.coordinator, self.backend, self.jobs,
        )
        self.repository_sync.reactions = self.reactions
        self.repository_add = RepositoryAddSupervisor(database, self.registry, workspaces, self.coordinator)
        self.bulk_repositories = BulkRepositoryUpdater(
            database, self.registry, self.coordinator, self.repositories,
        )
        self.bulk_repositories.reactions = self.reactions
        self.source_review = SourceReviewService(self.registry, workspaces, self.provisioner, self.configuration)
        self.ingress = WebhookIngress(self.coordinator, self.registry, self.backend)
        self.coordinator.ingress = self.ingress

    def handle(self, request: dict) -> dict:
        operation = validate_request(request)
        if operation == "protocol_info":
            return protocol_info()
        # A committed but incompletely published sync is a global barrier.
        # The operator can explicitly request reconciliation/replay.
        if (operation not in {"reconcile", "transition_status", "transition_list", "diagnostics_snapshot"}
                and not (operation == "transition_submit" and
                         request["action"] == "reconcile")):
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
        if operation == "transition_submit":
            return self.transitions.submit(request["action"], request["project_id"],
                                           request["request_key"])
        if operation == "transition_status":
            return self.transitions.get(request["id"])
        if operation == "transition_list":
            return self.transitions.list()
        if operation == "diagnostics_snapshot":
            minutes = request["minutes"]
            if type(minutes) is not int or minutes not in (1, 5, 30):
                raise ProtocolError("Invalid diagnostic window")
            try:
                events = self.diagnostics.snapshot(minutes)
                controller_status = "available"
            except Exception:
                events = {"events": [], "truncated": False}
                controller_status = "unavailable"
            try:
                runtime = self.transitions.diagnostics(minutes)
                runtime_status = "available"
            except Exception:
                runtime = {"events": [], "truncated": False}
                runtime_status = "unavailable"
            return {"events": events["events"] + runtime["events"],
                    "truncated": events["truncated"] or runtime["truncated"],
                    "sources": {"controller": controller_status, "runtime": runtime_status}}
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
                "last_repository_update": self._last_repository_update(identifier),
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
        if operation == "branch_preview":
            return self.repositories.branch_preview(
                request["project_id"], request["branch"], request["repository_id"],
            )
        if operation == "branch_apply":
            return self.repositories.branch_apply(
                request["project_id"], request["branch"],
                request["repository_id"], request["revision"],
            )
        if operation == "repository_bulk_submit":
            return self.bulk_repositories.submit(
                request["project_id"], request["request_key"],
            )
        if operation == "repository_bulk_status":
            return self.bulk_repositories.get(request["project_id"], request["id"])
        if operation == "repository_status":
            return {**self.repositories.status(request["project_id"], request["repository_id"]),
                    "sync": self.repository_sync.state(request["project_id"], request["repository_id"])}
        if operation == "repository_reactions":
            return self.reactions.get(request["project_id"], request["repository_id"])
        if operation == "repository_reaction_policy":
            return self.reactions.set_policy(
                request["project_id"], request["repository_id"],
                request["service"], request["action"],
            )
        if operation == "repository_sync_state":
            return self.repository_sync.state(request["project_id"], request["repository_id"])
        if operation == "repository_sync_policy":
            return self.repository_sync.set_policy(
                request["project_id"], request["repository_id"], request["policy"],
            )
        if operation == "repository_check":
            return self.repository_sync.perform(request["project_id"], request["repository_id"])
        if operation == "repository_update":
            result = self.repository_sync.perform(
                request["project_id"], request["repository_id"],
                update=True, expected_head=request["expected_head"],
            )
            self.reactions.dispatch_pending()
            return result
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
        if operation == "settings_review":
            return self.configuration.settings_review(request["project_id"])
        if operation == "settings_replace_checked":
            return self.configuration.replace_settings(
                request["project_id"], request["applications"], request["tasks"],
                expected_revision=request["revision"],
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
            return self._reconcile()
        if operation == "task":
            return {"job_id": self.jobs.submit_task(request["project_id"], request["name"])}
        if operation == "compose":
            return {"job_id": self.jobs.submit_compose(
                request["project_id"], request["action"])}
        if operation == "compose_service":
            return {"job_id": self.jobs.submit_service(
                request["project_id"], request["service"], request["action"],
            )}
        if operation == "adhoc":
            return {"job_id": self.jobs.submit_adhoc(
                request["project_id"], request["kind"], request["service"],
                request["image"], request["network"], request["command"],
            )}
        if operation == "private_ports":
            return self.private_ports.list(request["project_id"])
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

    def _reconcile(self, *, expected: dict | None = None) -> dict:
        """Single full explicit recovery path for web and durable CLI requests.

        Reconcile Docker before source publication, then verify the registry.
        Always use the same fail-closed coordinator and recovery services.
        """
        self.coordinator.reconcile(expected=expected)
        self.source_review.recover_pending()
        self.repository_add.reconcile()
        self.bulk_repositories.recover()
        return self._public_status(self.coordinator.status())

    def _last_repository_update(self, identifier: str) -> dict | None:
        with self.registry.database.transaction() as con:
            row = con.execute(
                "SELECT id,status FROM repository_bulk_updates "
                "WHERE project_id=? ORDER BY started_ns DESC,id DESC LIMIT 1",
                (identifier,),
            ).fetchone()
        return dict(row) if row else None

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
            # Only static event names, never project names, argv, keys or stdout.
            codes = {
                "transition_submit": "runtime-request",
                "reconcile": "runtime-reconcile",
                "project_create": "project-create",
                "import_submit": "project-import",
                "repository_update": "repository-update",
                "repository_check": "repository-check",
                "repository_sync_policy": "repository-sync-policy",
                "repository_add_submit": "repository-add",
                "repository_bulk_submit": "repository-bulk-update",
                "branch_apply": "repository-branch",
                "compose": "compose-operation", "adhoc": "job-submit",
                "task": "job-submit",
            }
            if isinstance(data, dict) and data.get("op") in codes:
                try:
                    self.server.controller.diagnostics.record(
                        "controller", codes[data["op"]])
                except Exception:
                    pass  # Recording must not change operation outcomes.
            response = {"ok": True, "result": result}
        except (Exception) as exc:
            try:
                self.server.controller.diagnostics.record("controller", "controller-error", "error")
            except Exception:
                pass
            # Controller interface is local/private but must not expose paths,
            # process output, command argv or environment secrets on errors.
            response = {"ok": False, "error": type(exc).__name__}
        # Never emit an oversized or unserializable reply. Clients have the
        # same fixed frame limit as the server; fail with a typed rejection.
        try:
            encoded = json.dumps(response, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeError):
            encoded = b'{"ok":false,"error":"ResponseSerializationError"}\n'
        if len(encoded) > _MAX_MESSAGE:
            encoded = b'{"ok":false,"error":"ResponseTooLarge"}\n'
        self.wfile.write(encoded)


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
    """Compatibility wrapper for older internal callers.

    All socket framing and error parsing now live in devstack.ipc.client.
    """
    try:
        return send_payload(location, payload, timeout=timeout)
    except (ControllerRejected, ControllerUnavailable) as exc:
        raise ProtocolError("Controller request failed") from exc


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
        controller.transitions.recover_interrupted()
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
        try:
            # Unlike Docker/source publication, unfinished Git batches are
            # never resumed after restart, even if runtime recovery failed.
            controller.bulk_repositories.recover()
        except Exception:
            pass
        try:
            controller.repository_sync.recover()
            controller.reactions.recover()
        except Exception:
            pass
        try:
            controller.diagnostics.record("controller", "controller-started")
        except Exception:
            pass
        with ControlServer(args.socket, controller, args.socket_group) as server:
            sync_stop = threading.Event()
            def poll_repositories():
                while not sync_stop.wait(30):
                    try:
                        controller.repository_sync.poll_once()
                        controller.reactions.dispatch_pending()
                    except Exception:
                        pass
            sync_thread = threading.Thread(target=poll_repositories,
                                           name="devstack-git-poll", daemon=True)
            sync_thread.start()
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
                    try:
                        controller.diagnostics.record("controller", "controller-stopping")
                    except Exception:
                        pass
                    sync_stop.set()
                    sync_thread.join(timeout=3)
                    public_ingress.shutdown()
                    webhook_thread.join(timeout=10)
                    try:
                        controller.coordinator.reconcile()
                    except Exception:
                        pass


if __name__ == "__main__":
    main()
