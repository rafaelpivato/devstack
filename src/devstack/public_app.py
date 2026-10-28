"""Shared unprivileged public HTTP ingress for MCP and project webhooks.

Tailscale Funnel proxies public HTTPS 443 to loopback 8768. This application
is NOT the private admin Web app and has no Docker or admin IPC access.
Webhook control packets use a separately permissioned Unix socket; request
bodies go directly to the validated private Docker service address.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from http.client import HTTPException
from ipaddress import ip_address
import json
import os
import re
import socket
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote
from urllib.request import (Request as URLRequest, ProxyHandler,
                            HTTPRedirectHandler, build_opener)

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, JSONResponse, RedirectResponse
from starlette.routing import Route, Mount

from devstack.control.ingress import _HEADERS, _MAX_BODY
from devstack.mcp_gateway import make_server, transport_security, public_host
from devstack.oauth import OAuthStore, OAuthError, CLIENT_ID, SCOPE

INGRESS_SOCKET = "/run/devstack-control/ingress.sock"
_ALLOWED_ROUTE = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
_MAX_FRAME = 2048
_SLOTS = threading.BoundedSemaphore(4)


class IngressRejected(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def ingress_call(op: str, **fields) -> dict:
    """Only admit/release. Cannot send arbitrary admin controller operations."""
    if op == "admit" and set(fields) == {"project_id", "route"}:
        payload = {"op": op, **fields}
    elif op in ("release", "renew") and set(fields) == {"lease"}:
        payload = {"op": op, **fields}
    else:
        raise IngressRejected("InvalidRequest")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(5)
        conn.connect(INGRESS_SOCKET)
        conn.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        with conn.makefile("rb") as source:
            raw = source.readline(_MAX_FRAME + 1)
    if len(raw) > _MAX_FRAME or not raw.endswith(b"\n"):
        raise IngressRejected("InvalidResponse")
    result = json.loads(raw)
    if not isinstance(result, dict) or set(result) not in (
            {"ok", "result"}, {"ok", "error"}):
        raise IngressRejected("InvalidResponse")
    if result.get("ok") is not True:
        raise IngressRejected(str(result.get("error", "Unavailable")))
    return result["result"]


def _target_url(target: dict) -> str:
    if set(target) != {"lease", "host", "port", "destination"}:
        raise IngressRejected("InvalidDestination")
    host, port, path = target["host"], target["port"], target["destination"]
    parsed = ip_address(host)
    if (parsed.version != 4 or not parsed.is_private or parsed.is_loopback
            or parsed.is_link_local or parsed.is_unspecified
            or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(path, str) or not path.startswith("/")
            or path.startswith("//") or ".." in path.split("/")):
        raise IngressRejected("InvalidDestination")
    return f"http://{host}:{port}{path}"


class NoRedirect(HTTPRedirectHandler):
    """Relay application redirects to the webhook caller; never follow them."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_DIRECT_HTTP = build_opener(ProxyHandler({}), NoRedirect())


def _forward(target: dict, body: bytes, headers: dict[str, str]):
    url = _target_url(target)
    filtered = {key: value for key, value in headers.items()
                if key.lower() in _HEADERS and len(value) < 4096
                and "\r" not in value and "\n" not in value}
    req = URLRequest(url, data=body, headers=filtered, method="POST")
    try:
        with _DIRECT_HTTP.open(req, timeout=8) as upstream:
            status, data = upstream.status, upstream.read(_MAX_BODY + 1)
            kind = upstream.headers.get("Content-Type", "application/octet-stream")
    except HTTPError as exc:
        status, data, kind = (exc.code, exc.read(_MAX_BODY + 1),
                              exc.headers.get("Content-Type", "text/plain"))
    if len(data) > _MAX_BODY:
        raise IngressRejected("OversizeResponse")
    return Response(data, status_code=status,
                    media_type=kind.split(";", 1)[0] if kind else "text/plain",
                    headers={"Cache-Control": "no-store"})


async def webhook(request: Request) -> Response:
    project, route = request.path_params["project"], request.path_params["route"]
    if not _ALLOWED_ROUTE.fullmatch(route) or not re.fullmatch(
            r"[0-9a-f-]{36}", project):
        return Response(status_code=404)
    if request.url.query:
        return Response(status_code=404)
    length = request.headers.get("content-length", "")
    if not length.isdecimal():
        return Response(status_code=411)
    if int(length) > _MAX_BODY:
        return Response(status_code=413)
    if not _SLOTS.acquire(blocking=False):
        return Response(status_code=503)
    lease = None
    heartbeat = None
    try:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_BODY:
                return Response(status_code=413)
        try:
            target = await asyncio.to_thread(ingress_call, "admit",
                project_id=project, route=route)
            lease = target["lease"]
            async def renew():
                while True:
                    await asyncio.sleep(3)
                    await asyncio.to_thread(ingress_call, "renew", lease=lease)
            heartbeat = asyncio.create_task(renew())
            return await asyncio.to_thread(
                _forward, target, bytes(body), dict(request.headers.items()))
        except IngressRejected as exc:
            if exc.code in {"WebhookNotFound", "ProjectNotFound"}:
                return Response(status_code=404)
            if exc.code in {"WebhookInvalid", "ValueError", "InvalidRequest"}:
                return Response(status_code=400)
            return Response(status_code=503)
        except (OSError, ValueError, HTTPException, URLError, KeyError):
            return Response(status_code=503)
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            from contextlib import suppress
            with suppress(asyncio.CancelledError, IngressRejected, OSError):
                await heartbeat
        if lease:
            try:
                await asyncio.to_thread(ingress_call, "release", lease=lease)
            except (OSError, ValueError, IngressRejected):
                # A lost release is bounded by the controller's lease timeout.
                pass
        _SLOTS.release()


import sqlite3


def oauth_resource(hostname: str) -> str:
    if not hostname:
        # For localhost protocol tests only. The deployed hostname is pinned.
        return "http://127.0.0.1/mcp"
    return "https://" + public_host(hostname) + "/mcp"


def oauth_issuer(hostname: str) -> str:
    return "https://" + public_host(hostname) if hostname else "http://127.0.0.1"


def oauth_error(code: str, status: int = 400) -> Response:
    return JSONResponse({"error": code}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def oauth_routes(hostname: str, store: OAuthStore) -> list:
    issuer = oauth_issuer(hostname)
    resource = oauth_resource(hostname)

    async def resource_metadata(_request: Request):
        return JSONResponse({"resource": resource, "authorization_servers": [issuer],
                             "scopes_supported": [SCOPE]},
                            headers={"Cache-Control": "no-store"})

    async def server_metadata(_request: Request):
        return JSONResponse({
            "issuer": issuer,
            "authorization_endpoint": issuer + "/oauth/authorize",
            "token_endpoint": issuer + "/oauth/token",
            "authorization_response_iss_parameter_supported": True,
            "client_id_metadata_document_supported": True,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": [SCOPE, "offline_access"],
        }, headers={"Cache-Control": "no-store"})

    async def authorize(request: Request):
        pairs = list(request.query_params.multi_items())
        params = dict(pairs)
        if len(pairs) != len(params):
            return oauth_error("invalid_request")
        try:
            identifier = store.start(params, resource)
        except OAuthError as exc:
            return oauth_error(exc.code, 503 if exc.code in (
                "store_unavailable", "temporarily_unavailable") else 400)
        except (OSError, ValueError, sqlite3.Error):
            return oauth_error("temporarily_unavailable", 503)
        private_url = ("https://" + hostname + ":8443/administration/mcp/authorize"
                       + "?request_id=" + quote(identifier))
        return RedirectResponse(private_url, status_code=303,
                                headers={"Cache-Control": "no-store",
                                         "Referrer-Policy": "no-referrer"})

    async def token(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != \
                "application/x-www-form-urlencoded":
            return oauth_error("invalid_request", 415)
        raw = await request.body()
        if len(raw) > 4096:
            return oauth_error("invalid_request", 413)
        try:
            pairs = parse_qsl(raw.decode("ascii"), keep_blank_values=True,
                              strict_parsing=True, max_num_fields=12)
            payload = dict(pairs)
            if len(pairs) != len(payload) or set(payload) - {
                "grant_type", "client_id", "redirect_uri", "code", "code_verifier",
                "refresh_token", "resource", "scope"}:
                raise OAuthError("invalid_request")
            answer = store.exchange(payload, resource)
        except OAuthError as exc:
            return oauth_error(exc.code)
        except (ValueError, UnicodeError):
            return oauth_error("invalid_request")
        except (OSError, sqlite3.Error):
            return oauth_error("temporarily_unavailable", 503)
        return JSONResponse(answer, headers={"Cache-Control": "no-store",
                                             "Pragma": "no-cache"})

    return [
        Route("/.well-known/oauth-protected-resource", resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", server_metadata, methods=["GET"]),
        Route("/oauth/authorize", authorize, methods=["GET"]),
        Route("/oauth/token", token, methods=["POST"]),
    ]


class AuthenticatedMCP:
    """ASGI middleware so streaming MCP request bodies are never buffered."""

    def __init__(self, app, hostname: str, store: OAuthStore):
        self.app, self.store = app, store
        self.resource = oauth_resource(hostname)
        self.metadata_url = oauth_issuer(hostname) + "/.well-known/oauth-protected-resource/mcp"

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (
                scope.get("path") == "/mcp" or scope.get("path", "").startswith("/mcp/")):
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            authorization = headers.get(b"authorization", b"")
            valid = (authorization.startswith(b"Bearer ") and
                     self.store.valid(authorization[7:].decode("ascii", "ignore"), self.resource))
            if not valid:
                response = JSONResponse(
                    {"error": "unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate":
                             'Bearer resource_metadata="' + self.metadata_url
                             + '", scope="' + SCOPE + '"',
                             "Cache-Control": "no-store"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def health(_request: Request) -> Response:
    return Response(b'{"scope":"public-ingress"}', media_type="application/json")


def create_app(hostname: str = "", *, mcp_enabled: bool = True,
               webhooks_enabled: bool = True, oauth_store: OAuthStore | None = None):
    mcp = make_server() if mcp_enabled else None
    store = oauth_store if oauth_store is not None else OAuthStore()
    @asynccontextmanager
    async def lifespan(_app):
        if mcp is not None:
            async with mcp.session_manager.run():
                yield
        else:
            yield
    routes = [Route("/healthz", health, methods=["GET"])]
    if webhooks_enabled:
        routes.append(Route("/hooks/{project}/{route}", webhook, methods=["POST"]))
    if mcp is not None:
        routes.extend(oauth_routes(hostname, store))
        routes.append(Mount("/", app=mcp.streamable_http_app(
            stateless_http=True, json_response=True,
            max_request_body_size=16384,
            transport_security=transport_security(hostname))))
    app = Starlette(routes=routes, lifespan=lifespan)
    return AuthenticatedMCP(app, hostname, store) if mcp is not None else app


def main() -> None:
    parser = argparse.ArgumentParser(description="DevStack public HTTP ingress")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--public-host", default=os.getenv("DEVSTACK_MCP_PUBLIC_HOST", ""))
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port outside allowed range")
    try:
        hostname = public_host(args.public_host)
    except ValueError as exc:
        parser.error(str(exc))
    import uvicorn
    uvicorn.run(create_app(hostname,
                           mcp_enabled=os.getenv("DEVSTACK_PUBLIC_MCP", "0") == "1",
                           webhooks_enabled=os.getenv("DEVSTACK_PUBLIC_WEBHOOKS", "0") == "1"),
                host="127.0.0.1", port=args.port,
                access_log=False)


if __name__ == "__main__":
    main()
