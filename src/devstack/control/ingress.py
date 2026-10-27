"""Project-aware public webhook ingress with admission leases.

The ingress listener is a separate loopback HTTP surface belonging to the
privileged control process, NOT an administrative API. Only exact registered
POST routes are forwarded, while the global runtime's active generation holds
a durable in-flight lease. Switching must drain leases before stopping Docker.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import re
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from devstack.domain.models import project_id
from devstack.storage.projects import ProjectNotFound

_ROUTE = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_PATH = re.compile(r"/[A-Za-z0-9_./~-]{0,254}\Z")
_MAX_BODY = 256 * 1024
_HEADERS = {"content-type", "stripe-signature", "x-github-event",
            "x-github-delivery", "x-hub-signature-256",
            "x-shopify-hmac-sha256", "x-webhook-signature"}


class WebhookNotFound(LookupError):
    pass


class WebhookInactive(RuntimeError):
    pass


class WebhookInvalid(ValueError):
    pass


class WebhookBusy(RuntimeError):
    pass


class WebhookIngress:
    def __init__(self, coordinator, registry, backend):
        self.coordinator = coordinator
        self.registry = registry
        self.database = coordinator.database
        self.backend = backend

    def list(self, identifier: str) -> tuple[dict, ...]:
        self.registry.get(project_id(identifier))
        with self.database.transaction() as db:
            rows = db.execute(
                "SELECT route,service,port,destination FROM webhook_routes "
                "WHERE project_id=? ORDER BY route", (identifier,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def configure(self, identifier: str, route: str, service: str,
                  port: int, destination: str) -> dict:
        project_id(identifier)
        if not isinstance(route, str) or not _ROUTE.fullmatch(route):
            raise WebhookInvalid("Invalid webhook route")
        if not isinstance(service, str) or not _SERVICE.fullmatch(service):
            raise WebhookInvalid("Invalid Compose service")
        if type(port) is not int or not 1 <= port <= 65535:
            raise WebhookInvalid("Invalid webhook service port")
        if (not isinstance(destination, str) or not _PATH.fullmatch(destination)
                or destination.startswith("//") or ".." in destination.split("/")):
            raise WebhookInvalid("Invalid webhook destination")
        with self.coordinator.locked():
            self.registry.get(identifier)
            with self.database.transaction(write=True) as db:
                db.execute(
                    "INSERT INTO webhook_routes"
                    "(project_id,route,service,port,destination) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(project_id,route) DO UPDATE SET "
                    "service=excluded.service,port=excluded.port,"
                    "destination=excluded.destination",
                    (identifier, route, service, port, destination),
                )
        return {"route": route, "service": service,
                "port": port, "destination": destination}

    def remove(self, identifier: str, route: str) -> None:
        project_id(identifier)
        if not isinstance(route, str) or not _ROUTE.fullmatch(route):
            raise WebhookInvalid("Invalid webhook route")
        with self.coordinator.locked():
            self.registry.get(identifier)
            with self.database.transaction(write=True) as db:
                count = db.execute(
                    "DELETE FROM webhook_routes WHERE project_id=? AND route=?",
                    (identifier, route),
                ).rowcount
                if count != 1:
                    raise WebhookNotFound("Webhook route not registered")

    @contextmanager
    def admitted(self, identifier: str, route: str):
        """Serialize admission with transitions, not HTTP network latency."""
        project_id(identifier)
        if not isinstance(route, str) or not _ROUTE.fullmatch(route):
            raise WebhookNotFound("Unknown webhook route")
        lease = str(uuid4())
        with self.coordinator.locked():
            self.registry.get(identifier)
            with self.database.transaction() as db:
                row = db.execute(
                    "SELECT service,port,destination FROM webhook_routes "
                    "WHERE project_id=? AND route=?", (identifier, route),
                ).fetchone()
            if row is None:
                raise WebhookNotFound("Unknown webhook route")
            state = self.coordinator.status()
            if state["phase"] != "active" or state["project_id"] != identifier:
                raise WebhookInactive("Webhook project is inactive")
            if not state["generation"]:
                raise WebhookInactive("Missing activation generation")
            with self.database.transaction(write=True) as db:
                db.execute(
                    "INSERT INTO webhook_leases(id,project_id,generation,started_ns)"
                    " VALUES(?,?,?,?)",
                    (lease, identifier, state["generation"], time.time_ns()),
                )
            settings = dict(row)
            generation = state["generation"]
        try:
            yield settings, generation
        finally:
            # Must NOT acquire the global lock: deactivation holds it while
            # waiting for the running request to finish and remove this lease.
            with self.database.transaction(write=True) as db:
                db.execute("DELETE FROM webhook_leases WHERE id=?", (lease,))

    def begin_delivery(self, identifier: str, route: str) -> dict:
        """Authorize a public HTTP delivery; controller never receives its body.

        A lease prevents project switching until public delivery is released.
        Only the restricted ingress socket may call this method remotely.
        """
        project_id(identifier)
        if not isinstance(route, str) or not _ROUTE.fullmatch(route):
            raise WebhookNotFound("Unknown webhook route")
        with self.coordinator.locked():
            self.registry.get(identifier)
            with self.database.transaction() as db:
                row = db.execute(
                    "SELECT service,port,destination FROM webhook_routes "
                    "WHERE project_id=? AND route=?", (identifier, route),
                ).fetchone()
            if row is None:
                raise WebhookNotFound("Unknown webhook route")
            state = self.coordinator.status()
            if state["phase"] != "active" or state["project_id"] != identifier:
                raise WebhookInactive("Inactive project")
            generation = state.get("generation")
            if not generation:
                raise WebhookInactive("Missing generation")
            host = self.backend.webhook_target(identifier, generation, row["service"])
            parsed = ip_address(host)
            if not parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified:
                raise WebhookInvalid("Unsafe Docker target")
            lease = str(uuid4())
            with self.database.transaction(write=True) as db:
                db.execute(
                    "INSERT INTO webhook_leases(id,project_id,generation,started_ns) "
                    "VALUES(?,?,?,?)", (lease, identifier, generation, time.time_ns()),
                )
            return {"lease": lease, "host": host, "port": row["port"],
                    "destination": row["destination"]}

    def finish_delivery(self, lease: str) -> None:
        """Idempotent completion from the restricted public ingress IPC."""
        from uuid import UUID
        try:
            if not isinstance(lease, str) or len(lease) != 36 or str(UUID(lease)) != lease:
                raise ValueError()
        except (ValueError, AttributeError) as exc:
            raise WebhookInvalid("Invalid lease") from exc
        with self.database.transaction(write=True) as db:
            db.execute("DELETE FROM webhook_leases WHERE id=?", (lease,))

    def drain(self, identifier: str, *, seconds: float = 14) -> None:
        """Caller holds coordinator lock and has already revoked admission."""
        deadline = time.monotonic() + seconds
        while True:
            with self.database.transaction() as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM webhook_leases WHERE project_id=?",
                    (identifier,),
                ).fetchone()[0]
            if count == 0:
                return
            if time.monotonic() >= deadline:
                raise WebhookBusy("Could not verify that in-flight webhooks drained")
            time.sleep(.05)

    def clear_after_daemon_restart(self) -> None:
        """Called only while holding exclusive daemon lifetime + global lock."""
        with self.database.transaction(write=True) as db:
            db.execute("DELETE FROM webhook_leases")

    def forward(self, identifier: str, route: str, payload: bytes,
                headers: dict[str, str]) -> tuple[int, bytes, str]:
        if not isinstance(payload, bytes) or len(payload) > _MAX_BODY:
            raise WebhookInvalid("Webhook body too large")
        with self.admitted(identifier, route) as (config, generation):
            host = self.backend.webhook_target(
                identifier, generation, config["service"],
            )
            parsed_host = ip_address(host)
            if (not parsed_host.is_private or parsed_host.is_loopback
                    or parsed_host.is_link_local or parsed_host.is_unspecified):
                raise WebhookInvalid("Docker target address is unsafe")
            url = ("http://" + ("[" + host + "]" if parsed_host.version == 6 else host)
                   + ":" + str(config["port"]) + config["destination"])
            forwarded = {k: v for k, v in headers.items()
                         if k.lower() in _HEADERS and isinstance(v, str)
                         and len(v) < 4096 and "\r" not in v and "\n" not in v}
            forwarded["Content-Length"] = str(len(payload))
            outgoing = Request(url, data=payload, headers=forwarded, method="POST")
            try:
                with urlopen(outgoing, timeout=8) as response:
                    code = response.status
                    body = response.read(_MAX_BODY + 1)
                    kind = response.headers.get("Content-Type", "application/octet-stream")
            except HTTPError as exc:
                code = exc.code
                body = exc.read(_MAX_BODY + 1)
                kind = exc.headers.get("Content-Type", "text/plain")
            except (OSError, URLError) as exc:
                raise WebhookInactive("Webhook destination unavailable") from exc
            if len(body) > _MAX_BODY:
                raise WebhookInvalid("Webhook response too large")
            return code, body, kind


class WebhookServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, ingress: WebhookIngress, bind_port: int = 8767):
        super().__init__(("127.0.0.1", bind_port), _WebhookHandler)
        self.ingress = ingress
        self.slots = threading.BoundedSemaphore(4)


class _WebhookHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        # Never emit signed payloads, provider tokens or body contents.
        return

    def respond(self, status: int, content: bytes = b"", kind: str = "text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", kind[:120])
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if content:
            self.wfile.write(content)
        self.close_connection = True

    def do_GET(self):
        if self.path == "/healthz":
            self.respond(200, b'{"scope":"webhook-only"}', "application/json")
        else:
            self.respond(404)

    def do_POST(self):
        if not self.server.slots.acquire(blocking=False):
            self.respond(503)
            return
        try:
            path = urlsplit(self.path)
            parts = path.path.split("/")
            if path.query or path.fragment or len(parts) != 4 or parts[1] != "hooks":
                self.respond(404)
                return
            identifier, route = parts[2], parts[3]
            size = self.headers.get("Content-Length")
            if not size or not size.isdecimal() or len(size) > 8:
                self.respond(411)
                return
            length = int(size)
            if length > _MAX_BODY:
                self.respond(413)
                return
            self.connection.settimeout(12)
            data = self.rfile.read(length)
            if len(data) != length:
                self.respond(400)
                return
            try:
                code, body, kind = self.server.ingress.forward(
                    identifier, route, data, dict(self.headers.items()),
                )
            except (WebhookNotFound, ProjectNotFound):
                self.respond(404)
            except WebhookInactive:
                self.respond(503)
            except (WebhookInvalid, ValueError):
                self.respond(400)
            except WebhookBusy:
                self.respond(503)
            except Exception:
                self.respond(503)
            else:
                self.respond(code, body, kind)
        finally:
            self.server.slots.release()
