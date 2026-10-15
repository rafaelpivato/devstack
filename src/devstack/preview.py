"""Isolated, tailnet-only HTTP preview service (never the admin WSGI app).

Preview origin MUST use another port from the privileged dashboard. Only
explicitly registered Compose services can be proxied, by their declared port.
This daemon does not configure Serve or Funnel and cannot publish a service.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
from urllib import error, parse, request as urllib_request

from flask import Flask, Response, abort, request

from .applications import ApplicationError, load_applications
from .core import load_projects
from .docker_stacks import DockerError, compose_command, compose_name, compose_path, project_root

MAX_REQUEST = 5 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
TIMEOUT = 12
ID_RE = re.compile(r"^[a-f0-9]{12,64}$")
EXCLUDED = {"connection", "transfer-encoding", "keep-alive", "proxy-authenticate",
            "proxy-authorization", "te", "trailer", "upgrade", "host", "cookie",
            "authorization", "origin", "referer", "x-forwarded-for", "x-forwarded-host",
            "x-forwarded-proto", "x-forwarded-prefix", "content-length"}
RETURN_HEADERS = {"content-type", "content-encoding", "cache-control",
                  "etag", "last-modified", "expires", "content-disposition",
                  "vary", "accept-ranges", "location"}


class PreviewError(RuntimeError):
    """Preview service is unavailable, without leaking Docker internals."""


def _json_run(argv: list[str], timeout: int = 6):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                check=False)
        if result.returncode:
            raise PreviewError("Docker service is not available")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise PreviewError("Unable to inspect running Docker service") from exc


def _container(project: str, service: str) -> dict:
    if not compose_path(project).is_file():
        raise PreviewError("Compose configuration is unavailable")
    try:
        cmd = compose_command(project) + ["ps", "--status", "running", "-q", service]
        result = subprocess.run(cmd, cwd=project_root(project),
                                capture_output=True, text=True, timeout=8, check=False)
    except (DockerError, OSError, subprocess.TimeoutExpired) as exc:
        raise PreviewError("Compose service is unavailable") from exc
    ids = result.stdout.splitlines() if result.returncode == 0 else []
    if len(ids) != 1 or not ID_RE.fullmatch(ids[0].strip()):
        raise PreviewError("Expected exactly one running service container")
    data = _json_run(["docker", "inspect", "--type", "container", ids[0].strip()])
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise PreviewError("Unexpected Docker inspect result")
    container = data[0]
    labels = container.get("Config", {}).get("Labels", {}) or {}
    if (labels.get("com.docker.compose.project") != compose_name(project)
            or labels.get("com.docker.compose.service") != service
            or container.get("State", {}).get("Running") is not True):
        raise PreviewError("Container does not belong to the requested Compose service")
    return container


def _target(project: str, service: str, port: int) -> str:
    container = _container(project, service)
    networks = container.get("NetworkSettings", {}).get("Networks", {}) or {}
    if not isinstance(networks, dict) or len(networks) > 12:
        raise PreviewError("Invalid service networks")
    for details in networks.values():
        if not isinstance(details, dict):
            continue
        network_id = details.get("NetworkID", "")
        address = details.get("IPAddress", "")
        if not (isinstance(network_id, str) and ID_RE.fullmatch(network_id)
                and isinstance(address, str)):
            continue
        try:
            ip = ipaddress.ip_address(address)
            if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local:
                continue
        except ValueError:
            continue
        network = _json_run(["docker", "network", "inspect", network_id])
        if not isinstance(network, list) or len(network) != 1:
            continue
        entry = network[0]
        labels = entry.get("Labels", {}) or {}
        if (entry.get("Id") != network_id
                or labels.get("com.docker.compose.project") != compose_name(project)
                or labels.get("com.docker.compose.network") != "default"):
            continue
        return f"http://{address}:{port}"
    raise PreviewError("The service is not on the project's default Compose network")


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_HTTP = urllib_request.build_opener(urllib_request.ProxyHandler({}), _NoRedirect())


def _fetch(destination: str, base_path: str, remainder: str):
    if (request.content_length is not None and request.content_length > MAX_REQUEST):
        abort(413)
    body = request.get_data(cache=False)
    if len(body) > MAX_REQUEST:
        abort(413)
    path = parse.quote(remainder, safe="/:@!$&'()*+,;=-._~")
    try:
        query = request.query_string.decode("ascii", "strict")
    except UnicodeDecodeError:
        abort(400)
    if len(query) > 4096:
        abort(414)
    url = destination + "/" + path + ("?" + query if query else "")
    headers = {key: value for key, value in request.headers
               if key.lower() not in EXCLUDED and not key.lower().startswith("proxy-")}
    # Keep dev service cookies and admin credentials entirely separated even
    # though both Tailscale ports use the same hostname (cookies are not port scoped).
    headers["X-Forwarded-Prefix"] = base_path
    headers["X-Forwarded-Proto"] = "https"
    upstream = urllib_request.Request(url, data=body if body else None,
                                      method=request.method, headers=headers)
    try:
        try:
            resp = _HTTP.open(upstream, timeout=TIMEOUT)
        except error.HTTPError as exc:
            resp = exc  # Forward upstream 4xx/5xx/redirect responses unchanged.
        with resp:
            content = resp.read(MAX_RESPONSE + 1)
            if len(content) > MAX_RESPONSE:
                raise PreviewError("Upstream response exceeds 32 MiB")
            outgoing = {}
            for key, value in resp.headers.items():
                lower = key.lower()
                if lower in RETURN_HEADERS:
                    if lower == "location" and value.startswith("/") and not value.startswith("//"):
                        value = base_path + value
                    outgoing[key] = value
            outgoing["Cache-Control"] = "no-store"  # Dev previews should not be cached.
            outgoing["X-Content-Type-Options"] = "nosniff"
            outgoing["Referrer-Policy"] = "no-referrer"
            return Response(content, status=resp.status, headers=outgoing)
    except (error.URLError, TimeoutError, OSError) as exc:
        raise PreviewError("Could not contact the application container") from exc


def create_preview_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST
    if config:
        app.config.update(config)

    @app.route("/p/<project>/<application>/", defaults={"remainder": ""},
               methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    @app.route("/p/<project>/<application>/<path:remainder>",
               methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def proxy(project: str, application: str, remainder: str):
        if project not in load_projects():
            abort(404)
        try:
            apps = load_applications(project)
        except (ApplicationError, DockerError, OSError):
            abort(404)
        item = next((x for x in apps if x.name == application and x.port), None)
        if item is None:
            abort(404)
        try:
            destination = _target(project, item.service, item.port)
            return _fetch(destination, "/p/" + project + "/" + application, remainder)
        except PreviewError as exc:
            return {"error": str(exc)}, 502

    @app.get("/")
    def index():
        # The preview origin must never render or forward the admin dashboard.
        return {"scope": "private application previews",
                "message": "Choose a registered application from the Devstack dashboard."}

    @app.get("/healthz")
    def health():
        return {"status": "ok", "scope": "previews-only"}

    @app.after_request
    def remove_sensitive_headers(response):
        # Untrusted containers cannot overwrite the dashboard's session cookie.
        response.headers.pop("Set-Cookie", None)
        response.headers["X-Frame-Options"] = "DENY"
        return response

    return app


def main():
    # Local HTTP is for Tailscale Serve; never expose the listener publicly.
    create_preview_app().run(host="127.0.0.1", port=8766, debug=False)


if __name__ == "__main__":
    main()
