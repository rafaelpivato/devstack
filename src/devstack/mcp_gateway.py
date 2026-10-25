"""Opt-in DevStack MCP gateway: safe health information and echo diagnostics.

This initial, unauthenticated surface has NO controller IPC credentials.
Controller health is obtained only through the existing loopback web /healthz
endpoint. Never add project, Docker, filesystem or shell tools here without
a separate authenticated design review.
"""
from __future__ import annotations

import argparse
from http.client import HTTPConnection, HTTPException
from importlib.metadata import version
import json
import os
import re
import sys
from threading import Lock
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

DEFAULT_PORT = 8768
DEFAULT_WEB_PORT = 8765
MAX_PING_LENGTH = 80
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?" + r"\Z")


class PingState:
    """Transient diagnostic payload, shared by clients, never persisted."""

    def __init__(self):
        self._lock = Lock()
        self._last_ping: str | None = None

    def read(self) -> str | None:
        with self._lock:
            return self._last_ping

    def ping(self, value: str) -> str:
        if (not isinstance(value, str) or not 1 <= len(value) <= MAX_PING_LENGTH
                or not value.isprintable()):
            raise ValueError("Ping value must be 1-80 printable characters")
        with self._lock:
            self._last_ping = value
        return value


def probe_controller(web_port: int = DEFAULT_WEB_PORT) -> str:
    """Reach controller through web /healthz only. Never use privileged IPC."""
    connection = HTTPConnection("127.0.0.1", web_port, timeout=2)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        # No redirects and bounded parsing, even if loopback service is damaged.
        body = json.loads(response.read(1024))
        if not isinstance(body, dict):
            return "unavailable"
        if response.status == 200 and body.get("ok") is True \
                and body.get("controller") == "connected":
            return "connected"
        if response.status == 503 and body.get("controller") == "recovery_required":
            return "recovery_required"
    except (OSError, TimeoutError, HTTPException, ValueError, UnicodeError):
        pass
    finally:
        connection.close()
    return "unavailable"


def public_host(value: str) -> str:
    """Allow only a specific external hostname, never a wildcard."""
    value = value.strip().lower()
    if value and (not _HOST_RE.fullmatch(value) or ".." in value
                  or value.startswith("-") or value.endswith("-")):
        raise ValueError("Invalid public hostname")
    return value


def transport_security(hostname: str) -> TransportSecuritySettings:
    hostname = public_host(hostname)
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins: list[str] = []
    if hostname:
        hosts.extend([hostname, hostname + ":*"])
        origins.extend(["https://" + hostname, "https://" + hostname + ":10000"])
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


def make_server(
    state: PingState | None = None, *, web_port: int = DEFAULT_WEB_PORT,
) -> MCPServer:
    state = state if state is not None else PingState()
    mcp = MCPServer("DevStack")

    @mcp.tool(
        title="DevStack status",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def devstack_status() -> dict[str, str | None]:
        """Get the installed gateway version, live controller health and last ping.

        Controller health follows the existing web /healthz -> controller IPC
        path. It does not include project names, logs, file paths or secrets.
        """
        health = probe_controller(web_port)
        log_code = "status_ok" if health == "connected" else "status_" + health
        print("DEVSTACK_MCP " + log_code, file=sys.stderr, flush=True)
        return {
            "version": version("devstack"),
            "controller": health,
            "last_ping": state.read(),
        }

    @mcp.tool(
        title="DevStack ping",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False,
        ),
    )
    def devstack_ping(
        value: Annotated[str, Field(min_length=1, max_length=MAX_PING_LENGTH)],
    ) -> dict[str, str | bool]:
        """Store an ephemeral diagnostic ping and immediately return its pong.

        Ping modifies only in-memory diagnostic state. devstack_status can
        read it back. It never changes DevStack runtime state or a file.
        """
        result = state.ping(value)
        print("DEVSTACK_MCP ping_ok", file=sys.stderr, flush=True)
        return {"pong": result, "persistent": False}

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="DevStack MCP connectivity gateway")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    parser.add_argument("--public-host", default=os.getenv("DEVSTACK_MCP_PUBLIC_HOST", ""))
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("MCP port must be between 1024 and 65535")
    if not 1024 <= args.web_port <= 65535:
        parser.error("Web port must be between 1024 and 65535")
    try:
        security = transport_security(args.public_host)
    except ValueError as exc:
        parser.error(str(exc))
    print("DEVSTACK_MCP gateway_started", file=sys.stderr, flush=True)
    make_server(web_port=args.web_port).run(
        transport="streamable-http", host="127.0.0.1", port=args.port,
        stateless_http=True, json_response=True,
        max_request_body_size=16384, transport_security=security,
    )


if __name__ == "__main__":
    main()
