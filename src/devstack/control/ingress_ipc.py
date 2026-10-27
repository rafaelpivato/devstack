"""Minimal public-webhook control socket, NOT the administrative controller IPC.

Only three fixed operations exist: authorize a registered active-project
delivery, renew it and release its bounded generation lease. Never accepts HTTP bodies.
The systemd controller account owns this socket, group devstackpublic.
"""
from __future__ import annotations

import grp
import json
import os
from pathlib import Path
import socketserver

MAX_FRAME = 2048


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            frame = self.rfile.readline(MAX_FRAME + 1)
            if (not frame or len(frame) > MAX_FRAME or not frame.endswith(b"\n")):
                raise ValueError("Invalid frame")
            payload = json.loads(frame)
            if not isinstance(payload, dict):
                raise ValueError("Invalid request")
            if (set(payload) == {"op", "project_id", "route"}
                    and payload["op"] == "admit"):
                result = self.server.ingress.begin_delivery(
                    payload["project_id"], payload["route"])
            elif set(payload) == {"op", "lease"} and payload["op"] == "renew":
                self.server.ingress.renew_delivery(payload["lease"])
                result = {"renewed": True}
            elif set(payload) == {"op", "lease"} and payload["op"] == "release":
                self.server.ingress.finish_delivery(payload["lease"])
                result = {"released": True}
            else:
                raise ValueError("Forbidden operation")
            answer = {"ok": True, "result": result}
        except Exception as exc:
            # Only an error class, not exception text or path/host details.
            answer = {"ok": False, "error": type(exc).__name__}
        self.wfile.write(json.dumps(answer, separators=(",", ":")).encode() + b"\n")


class IngressControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, location: Path, ingress, group: str = "devstackpublic"):
        self.location = Path(location)
        if self.location.exists() or self.location.is_symlink():
            raise RuntimeError("Ingress socket exists")
        self.location.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        super().__init__(str(self.location), _Handler)
        self.ingress = ingress
        try:
            # Socket itself stays group-restricted; allow directory traversal
            # without opening the separate admin socket to this public group.
            os.chmod(self.location.parent, 0o755)
            os.chmod(self.location, 0o660)
            if group:
                try:
                    gid = grp.getgrnam(group).gr_gid
                except KeyError:
                    # First v0.7.2 -> v0.7.3 activation runs via an older
                    # updater. Its release-owned postinstall hook creates the
                    # dedicated group and restarts the controller shortly.
                    # Do not prevent local controller health before that hook.
                    gid = None
                if gid is not None:
                    os.chown(self.location, -1, gid)
        except Exception:
            self.server_close()
            raise

    def server_close(self):
        super().server_close()
        self.location.unlink(missing_ok=True)
