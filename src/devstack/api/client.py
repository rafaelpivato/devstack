"""Unprivileged HTTP-to-controller transport: no Docker or database imports."""

import json
import socket
from pathlib import Path


class ControllerUnavailable(RuntimeError):
    """The privileged controller's private socket cannot be reached."""


class ControllerRejected(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def send(socket_path: Path, operation: str, *, timeout: float = 30, **fields):
    """Send an allowlisted request over a size-bounded local Unix socket."""
    request = {"op": operation, **fields}
    data = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(data) > 64 * 1024:
        raise ControllerRejected("RequestTooLarge")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(data)
            reply = bytearray()
            while not reply.endswith(b"\n"):
                part = client.recv(4096)
                if not part or len(reply) + len(part) > 64 * 1024:
                    raise ControllerUnavailable("Invalid controller response")
                reply.extend(part)
    except (ConnectionError, OSError, TimeoutError) as exc:
        raise ControllerUnavailable("Controller is unavailable") from exc
    try:
        data = json.loads(reply)
        if not isinstance(data, dict) or type(data.get("ok")) is not bool:
            raise ValueError()
        if not data["ok"]:
            code = data.get("error")
            if not isinstance(code, str):
                raise ValueError()
            raise ControllerRejected(code)
        if "result" not in data:
            raise ValueError()
        return data["result"]
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ControllerRejected):
            raise
        raise ControllerUnavailable("Malformed controller response") from exc
