"""Single unprivileged client transport for HTTP and future SSH CLI callers.

Only Unix-domain IPC is used; no controller, Docker or state imports.
"""
from __future__ import annotations

import json
from pathlib import Path
import socket

from .protocol import (
    MAX_MESSAGE, PROTOCOL_MAJOR, PROTOCOL_MINOR, ProtocolError,
    validate_request,
)


class ControllerUnavailable(RuntimeError):
    """The privileged controller's local socket is unavailable or malformed."""


class ControllerRejected(ValueError):
    """An explicit sanitized controller-side rejection (exception class name)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ControllerIncompatible(RuntimeError):
    """The connected controller cannot satisfy the requested IPC version."""


def send_payload(socket_path: Path, payload: dict, *, timeout: float = 30):
    """Send one exact-shape JSON request and receive one size-bounded response."""
    try:
        validate_request(payload)
        data = json.dumps(
            payload, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (ProtocolError, TypeError, ValueError, UnicodeError) as exc:
        raise ControllerRejected("ProtocolError") from exc
    if len(data) > MAX_MESSAGE:
        raise ControllerRejected("RequestTooLarge")

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(data)
            reply = bytearray()
            while not reply.endswith(b"\n"):
                part = client.recv(4096)
                if not part or len(reply) + len(part) > MAX_MESSAGE:
                    raise ControllerUnavailable("Invalid controller response")
                reply.extend(part)
    except (ConnectionError, OSError, TimeoutError) as exc:
        raise ControllerUnavailable("Controller is unavailable") from exc

    try:
        response = json.loads(reply)
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise ValueError()
        if not response["ok"]:
            code = response.get("error")
            if not isinstance(code, str):
                raise ValueError()
            raise ControllerRejected(code)
        if "result" not in response:
            raise ValueError()
        return response["result"]
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ControllerRejected):
            raise
        raise ControllerUnavailable("Malformed controller response") from exc


def send(socket_path: Path, operation: str, *, timeout: float = 30, **fields):
    """Convenient shared operation client used by the web and forthcoming CLI."""
    return send_payload(socket_path, {"op": operation, **fields}, timeout=timeout)


def ensure_compatible(socket_path: Path, *, timeout: float = 5,
                      minimum_minor: int = 0) -> dict:
    """Explicit capability handshake; v0.5.2 controllers fail clearly.

    This is deliberately a separate call. The web's established one-request
    operations are not burdened with a second round trip per HTTP request.
    """
    try:
        info = send(socket_path, "protocol_info", timeout=timeout)
    except ControllerRejected as exc:
        if exc.code == "ProtocolError":
            raise ControllerIncompatible(
                "Controller does not support IPC version negotiation"
            ) from exc
        raise
    if (not isinstance(info, dict)
            or type(info.get("major")) is not int
            or type(info.get("minor")) is not int
            or info["major"] != PROTOCOL_MAJOR
            or info["minor"] < max(PROTOCOL_MINOR, minimum_minor)):
        raise ControllerIncompatible("Controller IPC version is incompatible")
    return info
