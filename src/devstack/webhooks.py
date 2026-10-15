"""Minimal public webhook router: explicit route -> registered Compose application.

The public WSGI app is separate from the private admin app. No web UI,
redirects, wildcard routes, arbitrary URLs, or request-supplied destinations.
The receiving application MUST verify provider signatures/tokens itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
from urllib import error, request as urllib_request

from flask import Flask, Response, abort, request

from .applications import ApplicationError, load_applications
from .core import load_projects
from .docker_stacks import DockerError, compose_command, compose_name, compose_path, managed_dir, project_root

MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 1024 * 1024
MAX_ROUTES = 25
ROUTE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
PATH_RE = re.compile(r"/[A-Za-z0-9_./-]{0,254}\Z")
ID_RE = re.compile(r"[a-f0-9]{12,64}\Z")
REMOVE_HEADERS = {"host", "cookie", "connection", "proxy-authorization",
                  "proxy-authenticate", "content-length", "x-forwarded-for",
                  "x-forwarded-host", "x-forwarded-proto", "forwarded",
                  "transfer-encoding", "te", "trailer", "upgrade"}


class WebhookError(RuntimeError):
    pass


@dataclass(frozen=True)
class Webhook:
    name: str
    application: str
    path: str


def checked_route(name: str, application: str, path: str) -> Webhook:
    if not isinstance(name, str) or not ROUTE_RE.fullmatch(name):
        raise WebhookError("Webhook name must be lowercase letters, numbers or dashes")
    if not isinstance(application, str) or not application or len(application) > 64:
        raise WebhookError("Select a registered application")
    if (not isinstance(path, str) or not PATH_RE.fullmatch(path)
            or ".." in path or "//" in path or "\\" in path):
        raise WebhookError("Target path must be an absolute URL path without queries or traversal")
    return Webhook(name, application, path)


def registry_path(project: str) -> Path:
    target = managed_dir(project) / "webhooks.toml"
    if target.is_symlink():
        raise WebhookError("Refusing symlinked webhook config")
    return target


def load_webhooks(project: str) -> tuple[Webhook, ...]:
    target = registry_path(project)
    if not target.exists():
        return ()
    if target.stat().st_size > 32768:
        raise WebhookError("Webhook config too large")
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise WebhookError("Invalid webhook configuration") from exc
    if set(data) - {"webhooks"} or not isinstance(data.get("webhooks", []), list):
        raise WebhookError("Unknown webhook configuration fields")
    raw = data.get("webhooks", [])
    if len(raw) > MAX_ROUTES:
        raise WebhookError("Too many webhook routes")
    names, entries = set(), []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "application", "path"}:
            raise WebhookError("Webhook entries require name, application and path")
        route = checked_route(item["name"], item["application"], item["path"])
        if route.name in names:
            raise WebhookError("Duplicate webhook name")
        names.add(route.name)
        entries.append(route)
    return tuple(entries)


def _save(project: str, items: tuple[Webhook, ...]) -> None:
    folder = managed_dir(project)
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    lines = ["# Public Funnel routes, disabled until Funnel is enabled explicitly."]
    for entry in items:
        lines += ["", "[[webhooks]]", "name = " + json.dumps(entry.name),
                  "application = " + json.dumps(entry.application),
                  "path = " + json.dumps(entry.path)]
    data = "\n".join(lines) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder,
                                     prefix=".webhooks-", delete=False) as stream:
        target = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        target.chmod(0o600)
        if load_webhooks_preview(data) != items:
            raise WebhookError("Webhook config serialization failed")
        os.replace(target, registry_path(project))
    finally:
        target.unlink(missing_ok=True)


def load_webhooks_preview(data: str) -> tuple[Webhook, ...]:
    # Validate an atomic replacement by parsing the same schema.
    parsed = tomllib.loads(data)
    return tuple(checked_route(e["name"], e["application"], e["path"])
                 for e in parsed.get("webhooks", []))


def add_webhook(project: str, name: str, application: str, path: str) -> None:
    new = checked_route(name, application, path)
    # A public forwarding rule can only target an explicitly registered API.
    try:
        apps = load_applications(project)
    except (ApplicationError, DockerError) as exc:
        raise WebhookError("Cannot read application registry") from exc
    if not any(a.name == application and a.port for a in apps):
        raise WebhookError("Select a registered application with a container port")
    folder = managed_dir(project)
    folder.mkdir(mode=0o700, exist_ok=True)
    lock = folder / ".webhooks.lock"
    if lock.is_symlink():
        raise WebhookError("Refusing symlinked webhook lock")
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        existing = load_webhooks(project)
        if any(x.name == name for x in existing):
            raise WebhookError("Webhook name already exists")
        if len(existing) >= MAX_ROUTES:
            raise WebhookError("Too many webhook routes")
        _save(project, (*existing, new))


def remove_webhook(project: str, name: str) -> None:
    if not ROUTE_RE.fullmatch(name or ""):
        raise WebhookError("Invalid webhook name")
    folder = managed_dir(project)
    lock = folder / ".webhooks.lock"
    if lock.is_symlink():
        raise WebhookError("Refusing symlinked webhook lock")
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        existing = load_webhooks(project)
        updated = tuple(e for e in existing if e.name != name)
        if len(updated) == len(existing):
            raise WebhookError("Webhook not found")
        _save(project, updated)


def _docker_json(args: list[str]) -> object:
    try:
        run = subprocess.run(args, capture_output=True, text=True,
                             check=False, timeout=8)
        if run.returncode:
            raise WebhookError("Docker inspect failed")
        return json.loads(run.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise WebhookError("Docker inspection unavailable") from exc


def _target(project: str, app) -> str:
    if not compose_path(project).is_file():
        raise WebhookError("Compose service is not configured")
    try:
        run = subprocess.run(
            compose_command(project) + ["ps", "--status", "running", "-q", app.service],
            cwd=project_root(project), capture_output=True, text=True, check=False,
            timeout=8,
        )
    except (OSError, DockerError, subprocess.TimeoutExpired) as exc:
        raise WebhookError("Compose service unavailable") from exc
    ids = run.stdout.splitlines() if run.returncode == 0 else []
    if len(ids) != 1 or not ID_RE.fullmatch(ids[0].strip()):
        raise WebhookError("Requires exactly one running application container")
    containers = _docker_json(["docker", "inspect", "--type", "container", ids[0].strip()])
    if not isinstance(containers, list) or len(containers) != 1:
        raise WebhookError("Invalid container metadata")
    container = containers[0]
    if not isinstance(container, dict):
        raise WebhookError("Invalid container")
    labels = container.get("Config", {}).get("Labels", {}) or {}
    if (labels.get("com.docker.compose.project") != compose_name(project)
            or labels.get("com.docker.compose.service") != app.service
            or container.get("State", {}).get("Running") is not True):
        raise WebhookError("Container does not match the selected service")
    networks = container.get("NetworkSettings", {}).get("Networks", {}) or {}
    if not isinstance(networks, dict):
        raise WebhookError("Invalid container network")
    import ipaddress
    for details in networks.values():
        if not isinstance(details, dict):
            continue
        network_id = details.get("NetworkID", "")
        address = details.get("IPAddress", "")
        if not isinstance(network_id, str) or not ID_RE.fullmatch(network_id):
            continue
        try:
            ip = ipaddress.ip_address(address)
            if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local:
                continue
        except ValueError:
            continue
        nets = _docker_json(["docker", "network", "inspect", network_id])
        if not isinstance(nets, list) or len(nets) != 1:
            continue
        network = nets[0]
        attrs = network.get("Labels", {}) if isinstance(network, dict) else {}
        if (attrs.get("com.docker.compose.project") == compose_name(project)
                and attrs.get("com.docker.compose.network") == "default"
                and network.get("Id") == network_id):
            return f"http://{address}:{app.port}"
    raise WebhookError("Application is not on the Compose default network")


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_HTTP = urllib_request.build_opener(urllib_request.ProxyHandler({}), _NoRedirect())


def create_webhook_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
    if config:
        app.config.update(config)

    @app.post("/hooks/<project>/<name>")
    def deliver(project: str, name: str):
        if project not in load_projects() or not ROUTE_RE.fullmatch(name):
            abort(404)
        try:
            route = next((x for x in load_webhooks(project) if x.name == name), None)
            if route is None:
                abort(404)
            target_app = next((x for x in load_applications(project)
                               if x.name == route.application and x.port), None)
            if target_app is None:
                abort(404)
            target = _target(project, target_app)
            # Do not follow redirects, inherit credentials, accept arbitrary
            # upstream destinations, or perform URL rewriting.
            body = request.get_data(cache=False)
            if len(body) > MAX_BODY:
                abort(413)
            headers = {
                k: v for k, v in request.headers
                if k.lower() not in REMOVE_HEADERS and not k.lower().startswith("proxy-")
            }
            headers["X-Forwarded-Proto"] = "https"
            req = urllib_request.Request(target + route.path, data=body,
                                         method="POST", headers=headers)
            try:
                try:
                    response = _HTTP.open(req, timeout=10)
                except error.HTTPError as exc:
                    response = exc
                with response:
                    content = response.read(MAX_RESPONSE + 1)
                    if len(content) > MAX_RESPONSE:
                        raise WebhookError("Upstream response too large")
                    content_type = response.headers.get("Content-Type", "text/plain")
                    return Response(content, status=response.status,
                                    headers={"Content-Type": content_type,
                                             "Cache-Control": "no-store"})
            except (OSError, error.URLError, TimeoutError) as exc:
                raise WebhookError("Application did not answer the webhook") from exc
        except (WebhookError, ApplicationError, DockerError) as exc:
            # No internal details or credential material go to the public caller.
            return {"error": "Webhook destination unavailable"}, 502

    @app.get("/healthz")
    def health():
        return {"status": "ok", "scope": "webhook-only"}

    @app.after_request
    def public_headers(response):
        response.headers.pop("Set-Cookie", None)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return app
