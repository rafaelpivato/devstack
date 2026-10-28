"""Standards-shaped OAuth endpoints for the opt-in public MCP transport.

Only the known ChatGPT CIMD public client and stable RFC 9207 callback are
accepted. The human owner approves in private Serve :8443, never on Funnel.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode, parse_qs

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from devstack.oauth_store import (
    ACCESS_TTL, CLIENT_ID, OAuthError, OAuthStore, REDIRECT_URI, SCOPE,
)

_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
_CODE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


def auth_routes(hostname: str, store: OAuthStore) -> list:
    # Published paths must derive exclusively from the pinned Tailscale FQDN.
    issuer = "https://" + hostname
    resource = issuer + "/mcp"

    async def protected_resource(_request: Request):
        return JSONResponse({
            "resource": resource, "authorization_servers": [issuer],
            "scopes_supported": [SCOPE],
        }, headers={"Cache-Control": "no-store"})

    async def server_metadata(_request: Request):
        return JSONResponse({
            "issuer": issuer,
            "authorization_endpoint": issuer + "/oauth/authorize",
            "token_endpoint": issuer + "/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "scopes_supported": [SCOPE],
        }, headers={"Cache-Control": "no-store"})

    async def authorize(request: Request):
        q = request.query_params
        if (q.get("response_type") != "code"
                or q.get("code_challenge_method") != "S256"
                or q.get("client_id") != CLIENT_ID
                or q.get("redirect_uri") != REDIRECT_URI
                or q.get("resource") != resource
                or not _CHALLENGE.fullmatch(q.get("code_challenge", ""))
                or any(len(q.get(x, "")) > 1024 for x in q)):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            identifier = store.begin(
                challenge=q["code_challenge"], client=q["client_id"],
                redirect=q["redirect_uri"], state=q.get("state", ""),
                resource=resource, scope=q.get("scope") or SCOPE,
            )
        except OAuthError:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        except (OSError, ValueError, TypeError):
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
        # The browser must have tailnet access. Public Funnel never sees
        # the private approval page or receives an administrator cookie.
        destination = issuer + ":8443/administration/mcp?" + urlencode(
            {"oauth_request": identifier})
        return RedirectResponse(destination, status_code=303, headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        })

    async def token(request: Request):
        if request.headers.get("content-length", "0").isdigit() and int(
                request.headers.get("content-length", "0")) > 4096:
            return JSONResponse({"error": "invalid_request"}, status_code=413)
        if request.headers.get("content-type", "").split(";", 1)[0] != (
                "application/x-www-form-urlencoded"):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            raw = await request.body()
            if len(raw) > 4096:
                raise OAuthError("invalid_request")
            params = parse_qs(raw.decode("ascii"), keep_blank_values=True,
                              strict_parsing=True, max_num_fields=12)
            if any(len(values) != 1 for values in params.values()):
                raise OAuthError("invalid_request")
            form = {key: values[0] for key, values in params.items()}
            if (form.get("client_id") != CLIENT_ID
                    or form.get("resource") != resource
                    or request.headers.get("authorization")):
                raise OAuthError("invalid_client")
            if form.get("grant_type") == "authorization_code":
                if not _CODE.fullmatch(str(form.get("code", ""))):
                    raise OAuthError("invalid_grant")
                result = store.exchange(
                    code=str(form["code"]), verifier=str(form.get("code_verifier", "")),
                    client=str(form["client_id"]),
                    redirect=str(form.get("redirect_uri", "")),
                    resource=resource,
                )
            elif form.get("grant_type") == "refresh_token":
                if not _CODE.fullmatch(str(form.get("refresh_token", ""))):
                    raise OAuthError("invalid_grant")
                result = store.rotate(
                    token=str(form["refresh_token"]),
                    client=str(form["client_id"]), resource=resource,
                )
            else:
                raise OAuthError("unsupported_grant_type")
            return JSONResponse(result, headers={"Cache-Control": "no-store",
                                                 "Pragma": "no-cache"})
        except OAuthError as exc:
            status = 400
            return JSONResponse({"error": str(exc)}, status_code=status,
                                headers={"Cache-Control": "no-store"})
        except (OSError, ValueError, KeyError, TypeError):
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=503,
                                headers={"Cache-Control": "no-store"})

    return [
        Route("/.well-known/oauth-protected-resource", protected_resource, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", server_metadata, methods=["GET"]),
        Route("/oauth/authorize", authorize, methods=["GET"]),
        Route("/oauth/token", token, methods=["POST"]),
    ]


class ProtectedMCP:
    """Gate every MCP HTTP request before invoking SDK transport or listing tools."""

    def __init__(self, app, *, hostname: str, store: OAuthStore):
        self.app = app
        self.store = store
        self.resource = "https://" + hostname + "/mcp"
        self.metadata = ("https://" + hostname
                         + "/.well-known/oauth-protected-resource")

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or not (
                scope.get("path") == "/mcp" or
                scope.get("path", "").startswith("/mcp/"))):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("ascii", "ignore")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not self.store.validate(token, self.resource):
            response = JSONResponse(
                {"error": "unauthorized"}, status_code=401,
                headers={
                    "WWW-Authenticate":
                        'Bearer resource_metadata="' + self.metadata
                        + '", scope="' + SCOPE + '"',
                    "Cache-Control": "no-store",
                },
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
