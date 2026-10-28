"""Minimal single-owner OAuth grant store shared by the private approval UI and public MCP.

No activity analytics: only connection creation and credential expiry are persisted.
The root installer provisions the directory and lock with the narrowly scoped
devstackoauth Unix group. Neither process opens the controller IPC socket.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlencode

STORE = Path("/var/lib/devstack-oauth/state.json")
CLIENT_ID = "https://chatgpt.com/oauth/client.json"
REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
SCOPE = "devstack:mcp"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
PENDING_TTL = 600
CODE_TTL = 120
MAX_PENDING = 32
MAX_CONNECTIONS = 32


class OAuthError(ValueError):
    """Only fixed error codes; never expose credentials or stored metadata."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_state() -> dict:
    return {"pending": {}, "codes": {}, "grants": {}, "access": {}, "refresh": {}}


def _secret() -> str:
    return secrets.token_urlsafe(32)


class OAuthStore:
    def __init__(self, path: Path = STORE):
        self.path = Path(path)

    @contextmanager
    def _lock(self, exclusive: bool):
        # Fail closed if root-owned provisioning did not run.
        fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> dict:
        if self.path.is_symlink():
            raise OAuthError("unsafe_store")
        with self.path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or set(value) != set(_new_state()):
            raise OAuthError("invalid_store")
        if any(not isinstance(v, dict) for v in value.values()):
            raise OAuthError("invalid_store")
        return value

    def _save(self, data: dict) -> None:
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix=".oauth-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fchmod(handle.fileno(), 0o660)
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _read(self, action):
        with self._lock(False):
            return action(self._load())

    def _change(self, action):
        with self._lock(True):
            data = self._load()
            now = time.time()
            for collection in ("pending", "codes", "access", "refresh"):
                data[collection] = {
                    key: row for key, row in data[collection].items()
                    if isinstance(row, dict) and row.get("expires", 0) > now
                }
            result = action(data)
            self._save(data)
            return result

    def begin(self, *, challenge: str, client: str, redirect: str,
              state: str, resource: str, scope: str) -> str:
        if client != CLIENT_ID or redirect != REDIRECT_URI or scope != SCOPE:
            raise OAuthError("invalid_client")
        if not (43 <= len(challenge) <= 128 and
                all(c.isalnum() or c in "-_" for c in challenge)):
            raise OAuthError("invalid_request")
        if not (8 <= len(state) <= 512 and state.isascii()):
            raise OAuthError("invalid_request")
        identifier = _secret()
        def save(data):
            if len(data["pending"]) >= MAX_PENDING:
                raise OAuthError("too_many_requests")
            data["pending"][identifier] = {
                "challenge": challenge, "client": client, "redirect": redirect,
                "state": state, "resource": resource, "scope": scope,
                "created_at": _timestamp(), "expires": time.time() + PENDING_TTL,
            }
            return identifier
        return self._change(save)

    def pending(self, identifier: str) -> dict:
        def read(data):
            row = data["pending"].get(identifier)
            if not row or row["expires"] <= time.time():
                raise OAuthError("request_not_found")
            return {"id": identifier, "client": "ChatGPT",
                    "created_at": row["created_at"], "scope": row["scope"]}
        return self._read(read)

    def decide(self, identifier: str, *, approve: bool, issuer: str) -> str:
        def decide(data):
            row = data["pending"].pop(identifier, None)
            if not row:
                raise OAuthError("request_not_found")
            redirect = row["redirect"]
            if not approve:
                return redirect + "?" + urlencode({
                    "error": "access_denied", "state": row["state"], "iss": issuer,
                })
            if len(data["grants"]) >= MAX_CONNECTIONS:
                raise OAuthError("too_many_connections")
            grant = _secret()
            code = _secret()
            data["grants"][grant] = {
                "client": row["client"], "scope": row["scope"],
                "resource": row["resource"], "created_at": _timestamp(),
            }
            data["codes"][_hash(code)] = {
                "grant": grant, "challenge": row["challenge"],
                "redirect": redirect, "expires": time.time() + CODE_TTL,
            }
            return redirect + "?" + urlencode({
                "code": code, "state": row["state"], "iss": issuer,
            })
        return self._change(decide)

    @staticmethod
    def _tokens(data: dict, grant: str) -> dict:
        access, refresh = _secret(), _secret()
        now = time.time()
        data["access"][_hash(access)] = {
            "grant": grant, "expires": now + ACCESS_TTL,
        }
        data["refresh"][_hash(refresh)] = {
            "grant": grant, "expires": now + REFRESH_TTL,
        }
        return {"access_token": access, "refresh_token": refresh,
                "token_type": "Bearer", "expires_in": ACCESS_TTL, "scope": SCOPE}

    def exchange(self, *, code: str, verifier: str, client: str,
                 redirect: str, resource: str) -> dict:
        if client != CLIENT_ID or redirect != REDIRECT_URI:
            raise OAuthError("invalid_client")
        if not (43 <= len(verifier) <= 128 and verifier.isascii() and
                all(c.isalnum() or c in "-._~" for c in verifier)):
            raise OAuthError("invalid_grant")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        def exchange(data):
            row = data["codes"].get(_hash(code))
            if not row or row["redirect"] != redirect or not hmac.compare_digest(
                    row["challenge"], challenge):
                raise OAuthError("invalid_grant")
            grant = row["grant"]
            if data["grants"].get(grant, {}).get("resource") != resource:
                raise OAuthError("invalid_grant")
            del data["codes"][_hash(code)]
            return self._tokens(data, grant)
        return self._change(exchange)

    def rotate(self, *, token: str, client: str, resource: str) -> dict:
        if client != CLIENT_ID:
            raise OAuthError("invalid_client")
        def rotate(data):
            row = data["refresh"].get(_hash(token))
            if not row:
                raise OAuthError("invalid_grant")
            grant = row["grant"]
            if data["grants"].get(grant, {}).get("resource") != resource:
                raise OAuthError("invalid_grant")
            del data["refresh"][_hash(token)]
            # Revoke preceding access tokens for the same client grant.
            data["access"] = {
                key: val for key, val in data["access"].items()
                if val["grant"] != grant
            }
            return self._tokens(data, grant)
        return self._change(rotate)

    def validate(self, token: str, resource: str) -> bool:
        if not token or len(token) > 256 or not token.isascii():
            return False
        def read(data):
            row = data["access"].get(_hash(token))
            return bool(row and row["expires"] > time.time()
                        and data["grants"].get(row["grant"], {}).get("resource") == resource
                        and data["grants"][row["grant"]].get("scope") == SCOPE)
        try:
            return self._read(read)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def connections(self) -> list[dict]:
        def read(data):
            return sorted([
                {"id": identifier, "name": "ChatGPT",
                 "created_at": row["created_at"], "scope": row["scope"]}
                for identifier, row in data["grants"].items()
            ], key=lambda row: row["created_at"], reverse=True)
        return self._read(read)

    def revoke(self, identifier: str) -> None:
        def revoke(data):
            if identifier not in data["grants"]:
                raise OAuthError("connection_not_found")
            del data["grants"][identifier]
            for collection in ("codes", "access", "refresh"):
                data[collection] = {key: row for key, row in data[collection].items()
                                    if row["grant"] != identifier}
        self._change(revoke)
