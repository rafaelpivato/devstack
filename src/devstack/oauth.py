"""Single-owner MCP OAuth state. No user accounts, login service, or usage tracking.

Only the private Tailnet dashboard approves requests. This module is shared by
the public gateway and private Web processes using a dedicated group-owned
SQLite file, never the administrative controller database or IPC socket.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

DB_PATH = Path("/var/lib/devstack-oauth/oauth.sqlite3")
CLIENT_ID = "https://chatgpt.com/oauth/client.json"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
SCOPE = "devstack:read"
_CHALLENGE = re.compile(r"[a-zA-Z0-9_-]{43,128}\Z")
_VALUE = re.compile(r"[a-zA-Z0-9_~-]{1,512}\Z")
PENDING_TTL = 600
CODE_TTL = 120
ACCESS_TTL = 900
REFRESH_TTL = 90 * 86400


class OAuthError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OAuthStore:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)

    def bootstrap(self) -> None:
        """Only installer/root should initialize the real deployment database."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise OAuthError("unsafe_store")
        with sqlite3.connect(self.path, timeout=3) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pending (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL, state TEXT NOT NULL,
                    challenge TEXT NOT NULL, resource TEXT NOT NULL,
                    scope TEXT NOT NULL, created REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS codes (
                    hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    challenge TEXT NOT NULL, resource TEXT NOT NULL,
                    scope TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connections (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, resource TEXT NOT NULL,
                    scope TEXT NOT NULL, access_hash TEXT UNIQUE,
                    access_expires REAL, refresh_hash TEXT UNIQUE,
                    refresh_expires REAL, revoked INTEGER NOT NULL DEFAULT 0
                );
            """)

    @contextmanager
    def _db(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise OAuthError("store_unavailable")
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=3000")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def authorize_parameters(params: dict, resource: str) -> dict:
        allowed = {"client_id", "redirect_uri", "response_type", "state",
                   "code_challenge", "code_challenge_method", "scope", "resource"}
        if set(params) - allowed:
            raise OAuthError("invalid_request")
        if params.get("client_id") != CLIENT_ID or params.get("redirect_uri") != CALLBACK:
            raise OAuthError("unauthorized_client")
        if params.get("response_type") != "code" or params.get("code_challenge_method") != "S256":
            raise OAuthError("invalid_request")
        if not _CHALLENGE.fullmatch(params.get("code_challenge", "")):
            raise OAuthError("invalid_request")
        state = params.get("state", "")
        if not state or len(state) > 512 or not state.isprintable():
            raise OAuthError("invalid_request")
        if params.get("resource") != resource:
            raise OAuthError("invalid_target")
        scope = params.get("scope", SCOPE)
        if set(scope.split()) - {SCOPE, "offline_access"} or SCOPE not in scope.split():
            raise OAuthError("invalid_scope")
        return {"client_id": CLIENT_ID, "redirect_uri": CALLBACK,
                "state": state, "challenge": params["code_challenge"],
                "resource": resource, "scope": scope}

    def start(self, params: dict, resource: str) -> str:
        accepted = self.authorize_parameters(params, resource)
        identifier = secrets.token_urlsafe(32)
        now = time.time()
        with self._db() as db:
            db.execute("DELETE FROM pending WHERE created < ?", (now - PENDING_TTL,))
            db.execute("DELETE FROM codes WHERE expires < ?", (now,))
            count = db.execute("SELECT count(*) FROM pending WHERE status = 'pending'").fetchone()[0]
            if count >= 50:
                raise OAuthError("temporarily_unavailable")
            db.execute("""INSERT INTO pending
                (id,client_id,redirect_uri,state,challenge,resource,scope,created)
                VALUES (?,?,?,?,?,?,?,?)""",
                (identifier, accepted["client_id"], accepted["redirect_uri"],
                 accepted["state"], accepted["challenge"], accepted["resource"],
                 accepted["scope"], now))
        return identifier

    def pending(self, identifier: str) -> dict:
        if not _VALUE.fullmatch(identifier):
            raise OAuthError("invalid_request")
        with self._db() as db:
            row = db.execute("SELECT * FROM pending WHERE id=?", (identifier,)).fetchone()
        if not row or row["status"] != "pending" or row["created"] + PENDING_TTL < time.time():
            raise OAuthError("request_expired")
        return {"id": identifier, "client": "ChatGPT", "scope": row["scope"],
                "created_at": datetime.fromtimestamp(row["created"], timezone.utc).isoformat()}

    def decide(self, identifier: str, approve: bool, issuer: str) -> str:
        if not isinstance(approve, bool):
            raise OAuthError("invalid_request")
        self.pending(identifier)
        with self._db() as db:
            row = db.execute("SELECT * FROM pending WHERE id=?", (identifier,)).fetchone()
            if not row or row["status"] != "pending" or row["created"] + PENDING_TTL < time.time():
                raise OAuthError("request_expired")
            if db.execute("UPDATE pending SET status=? WHERE id=? AND status='pending'",
                          ("approved" if approve else "denied", identifier)).rowcount != 1:
                raise OAuthError("request_expired")
            result = {"state": row["state"], "iss": issuer}
            if approve:
                code = secrets.token_urlsafe(32)
                db.execute("""INSERT INTO codes
                    (hash,client_id,challenge,resource,scope,expires)
                    VALUES (?,?,?,?,?,?)""",
                    (digest(code), row["client_id"], row["challenge"], row["resource"],
                     row["scope"], time.time() + CODE_TTL))
                result["code"] = code
            else:
                result["error"] = "access_denied"
        return CALLBACK + "?" + urlencode(result)

    def exchange(self, payload: dict, resource: str) -> dict:
        if payload.get("client_id") != CLIENT_ID or payload.get("resource") != resource:
            raise OAuthError("invalid_client")
        grant = payload.get("grant_type")
        now = time.time()
        with self._db() as db:
            if grant == "authorization_code":
                code = payload.get("code", "")
                verifier = payload.get("code_verifier", "")
                if (not 16 <= len(code) <= 256 or not _CHALLENGE.fullmatch(verifier)
                        or payload.get("redirect_uri") != CALLBACK):
                    raise OAuthError("invalid_grant")
                row = db.execute("SELECT * FROM codes WHERE hash=?", (digest(code),)).fetchone()
                if not row or row["expires"] < now or row["client_id"] != CLIENT_ID \
                        or row["resource"] != resource:
                    raise OAuthError("invalid_grant")
                import base64
                calculated = base64.urlsafe_b64encode(hashlib.sha256(
                    verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
                if not hmac.compare_digest(calculated, row["challenge"]):
                    raise OAuthError("invalid_grant")
                if db.execute("DELETE FROM codes WHERE hash=?", (digest(code),)).rowcount != 1:
                    raise OAuthError("invalid_grant")
                connection_id = secrets.token_hex(16)
                access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                db.execute("""INSERT INTO connections
                    (id,client_id,created_at,resource,scope,access_hash,access_expires,
                     refresh_hash,refresh_expires)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (connection_id, CLIENT_ID, _utc(), resource, row["scope"],
                     digest(access), now + ACCESS_TTL, digest(refresh), now + REFRESH_TTL))
            elif grant == "refresh_token":
                refresh_old = payload.get("refresh_token", "")
                if not 16 <= len(refresh_old) <= 256:
                    raise OAuthError("invalid_grant")
                row = db.execute("""SELECT * FROM connections WHERE refresh_hash=?
                    AND revoked=0""", (digest(refresh_old),)).fetchone()
                if not row or row["resource"] != resource or row["refresh_expires"] < now:
                    raise OAuthError("invalid_grant")
                access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                if db.execute("""UPDATE connections SET access_hash=?,access_expires=?,
                    refresh_hash=?,refresh_expires=? WHERE id=? AND refresh_hash=? AND revoked=0""",
                    (digest(access), now + ACCESS_TTL, digest(refresh),
                     now + REFRESH_TTL, row["id"], digest(refresh_old))).rowcount != 1:
                    raise OAuthError("invalid_grant")
            else:
                raise OAuthError("unsupported_grant_type")
        return {"access_token": access, "token_type": "Bearer",
                "expires_in": ACCESS_TTL, "refresh_token": refresh, "scope": SCOPE}

    def valid(self, token: str, resource: str) -> bool:
        if not isinstance(token, str) or not 16 <= len(token) <= 256:
            return False
        try:
            with self._db() as db:
                row = db.execute("""SELECT resource,scope,access_expires,revoked
                    FROM connections WHERE access_hash=?""", (digest(token),)).fetchone()
            return bool(row and row["revoked"] == 0 and row["resource"] == resource
                        and row["access_expires"] > time.time()
                        and SCOPE in row["scope"].split())
        except (sqlite3.Error, OAuthError):
            return False

    def connections(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("""SELECT id,created_at,client_id FROM connections
                WHERE revoked=0 AND refresh_expires>? ORDER BY created_at DESC LIMIT 100""",
                (time.time(),)).fetchall()
        return [{"id": row["id"], "client": "ChatGPT",
                 "created_at": row["created_at"]} for row in rows]

    def revoke(self, identifier: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise OAuthError("invalid_request")
        with self._db() as db:
            if db.execute("UPDATE connections SET revoked=1,access_hash=NULL,refresh_hash=NULL WHERE id=?",
                          (identifier,)).rowcount != 1:
                raise OAuthError("not_found")
