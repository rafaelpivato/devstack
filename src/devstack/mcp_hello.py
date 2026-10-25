"""DevStack v0.7 connectivity proof. Intentionally isolated from controller IPC.

Unauthenticated/public until the owner disables Tailscale Funnel. NEVER add
controller, filesystem, Docker or project operations to this prototype.
"""
from __future__ import annotations

import argparse
from importlib.metadata import version
import os
import re
from threading import Lock
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

DEFAULT_PORT = 8768
MAX_VALUE_LENGTH = 80
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\\Z")


class HelloState:
    """Process-local, ephemeral test value. No file, database or IPC access."""

    def __init__(self):
        self._lock = Lock()
        self._value = "world"

    def read(self) -> str:
        with self._lock:
            return self._value

    def write(self, value: str) -> str:
        if (not isinstance(value, str) or not 1 <= len(value) <= MAX_VALUE_LENGTH
                or not value.isprintable()):
            raise ValueError("Value must be 1-80 printable characters")
        with self._lock:
            self._value = value
            return self._value


def public_host(value: str) -> str:
    """Validate one explicit external hostname; never accept wildcard hosts."""
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


def make_server(state: HelloState | None = None) -> MCPServer:
    state = state or HelloState()
    mcp = MCPServer("DevStack Hello World")

    @mcp.tool(
        title="Read hello value",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def hello_read() -> dict[str, str]:
        """Read a harmless test value from this MCP process. No project data."""
        return {"greeting": "Hello from DevStack MCP", "value": state.read(),
                "version": version("devstack")}

    @mcp.tool(
        title="Write hello value",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False,
        ),
    )
    def hello_write(
        value: Annotated[str, Field(min_length=1, max_length=MAX_VALUE_LENGTH)],
    ) -> dict[str, str]:
        """Set an ephemeral greeting value, never writing to disk or DevStack."""
        return {"value": state.write(value), "persisted": "no"}

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="DevStack MCP hello-only gateway")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--public-host", default=os.getenv("DEVSTACK_MCP_PUBLIC_HOST", ""))
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    try:
        security = transport_security(args.public_host)
    except ValueError as exc:
        parser.error(str(exc))
    make_server().run(
        transport="streamable-http", host="127.0.0.1", port=args.port,
        stateless_http=True, json_response=True,
        max_request_body_size=16384, transport_security=security,
    )


if __name__ == "__main__":
    main()
