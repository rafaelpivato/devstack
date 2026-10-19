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
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database
from devstack.storage.projects import ProjectRegistry
from .coordinator import Coordinator
from .docker import DockerBackend
from .jobs import JobSupervisor

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

    def handle(self, request: dict) -> dict:
        if not isinstance(request, dict) or not isinstance(request.get("op"), str):
            raise ProtocolError("Expected a controller operation")
        operation = request["op"]
        fields = {
            "health": {"op"},
            "status": {"op"},
            "projects": {"op"},
            "project_create": {"op", "slug", "name"},
            "project_import": {"op", "url", "slug", "name"},
            "activate": {"op", "project_id"},
            "switch": {"op", "project_id"},
            "deactivate": {"op"},
            "reconcile": {"op"},
            "task": {"op", "project_id", "name"},
            "compose": {"op", "project_id", "action"},
            "job": {"op", "id"},
        }
        if operation not in fields or set(request) != fields[operation]:
            raise ProtocolError("Unexpected or missing controller parameters")
        if operation == "health":
            return {"alive": True, "runtime": self.coordinator.status()["phase"]}
        if operation == "status":
            return self._public_status(self.coordinator.status())
        if operation == "projects":
            return {"projects": [
                {"id": p.id, "slug": p.slug, "name": p.name}
                for p in self.registry.list()
            ]}
        if operation == "project_create":
            entry = self.provisioner.create_empty(request["slug"], request["name"])
            return {"id": entry.id, "name": entry.name}
        if operation == "project_import":
            entry = self.provisioner.import_github(
                request["url"], request["slug"], request["name"],
            )
            return {"id": entry.id, "name": entry.name}
        if operation == "activate":
            return self._public_status(self.coordinator.activate(request["project_id"]))
        if operation == "switch":
            return self._public_status(self.coordinator.switch(request["project_id"]))
        if operation == "deactivate":
            return self._public_status(self.coordinator.deactivate())
        if operation == "reconcile":
            return self._public_status(self.coordinator.reconcile())
        if operation == "task":
            return {"job_id": self.jobs.submit_task(request["project_id"], request["name"])}
        if operation == "compose":
            return {"job_id": self.jobs.submit_compose(
                request["project_id"], request["action"])}
        if operation == "job":
            return self.jobs.get(request["id"])
        raise ProtocolError("Unknown operation")

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
    args = parser.parse_args()
    with exclusive_daemon(args.state):
        database = Database(args.state / "control.sqlite3")
        workspaces = WorkspaceStore(args.state / "projects")
        controller = Controller(database, workspaces)
        try:
            controller.coordinator.reconcile()
        except Exception:
            # Remain available to report and explicitly recover ERROR.
            pass
        with ControlServer(args.socket, controller, args.socket_group) as server:
            def stop(_signum, _frame):
                threading.Thread(target=server.shutdown, daemon=True).start()
    
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            try:
                server.serve_forever(poll_interval=0.2)
            finally:
                try:
                    controller.coordinator.reconcile()
                except Exception:
                    pass
    

if __name__ == "__main__":
    main()
