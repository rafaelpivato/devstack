"""v0.7.4 single-owner MCP OAuth: private approval, PKCE, revocation, and isolation."""
import base64
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

from devstack.api.http import create_app as create_private
from devstack.oauth_store import OAuthError, OAuthStore, _new_state, CLIENT_ID, REDIRECT_URI, SCOPE
from devstack.public_app import create_app as create_public

HOST = "node.example.ts.net"
RESOURCE = "https://" + HOST + "/mcp"
ISSUER = "https://" + HOST
VERIFIER = "v" * 48
CHALLENGE = base64.urlsafe_b64encode(sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


class OAuthFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.path = root / "state.json"
        self.path.write_text(json.dumps(_new_state()), encoding="utf-8")
        self.path.with_suffix(".lock").write_text("", encoding="utf-8")
        self.store = OAuthStore(self.path)
        self.private = create_private({
            "TESTING": True, "MCP_OAUTH_STORE_PATH": str(self.path),
            "WEB_DIAGNOSTICS_PATH": str(root / "web-diag.sqlite3"),
            "WEB_DIAGNOSTICS_FALLBACK_PATH": str(root / "fallback.sqlite3"),
        })
        self.web = self.private.test_client()
        with self.web.session_transaction() as sess:
            sess["_csrf"] = "test-only-csrf"
        self.headers = {"X-CSRF-Token": "test-only-csrf"}
        self.public = TestClient(create_public(HOST, mcp_enabled=True,
                                                webhooks_enabled=False,
                                                auth_store=self.store))
        self.public.__enter__()

    def tearDown(self):
        self.public.__exit__(None, None, None)
        self.tmp.cleanup()

    def _request(self, **overrides):
        query = {
            "response_type": "code", "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI, "scope": SCOPE,
            "resource": RESOURCE, "state": "csrf-state-12345",
            "code_challenge_method": "S256", "code_challenge": CHALLENGE,
        }
        query.update(overrides)
        return self.public.get("/oauth/authorize", params=query, follow_redirects=False)

    def _approve(self):
        location = self._request().headers["location"]
        parsed = urlparse(location)
        self.assertEqual(parsed.netloc, HOST + ":8443")
        self.assertEqual(parsed.path, "/connect/authorize")
        identifier = parse_qs(parsed.query)["oauth_request"][0]
        pending = self.web.get("/api/v2/devstack/mcp/auth-requests/" + identifier)
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(pending.json["request"]["client"], "ChatGPT")
        forbidden = self.web.post(
            "/api/v2/devstack/mcp/auth-requests/" + identifier + "/approve", json={})
        self.assertEqual(forbidden.status_code, 403)
        approved = self.web.post(
            "/api/v2/devstack/mcp/auth-requests/" + identifier + "/approve",
            json={}, headers=self.headers)
        self.assertEqual(approved.status_code, 200)
        redirect = urlparse(approved.json["redirect"])
        self.assertEqual(redirect.scheme + "://" + redirect.netloc + redirect.path,
                         REDIRECT_URI)
        values = parse_qs(redirect.query)
        self.assertEqual(values["state"], ["csrf-state-12345"])
        self.assertEqual(values["iss"], [ISSUER])
        return values["code"][0]

    def _exchange(self, code, **changes):
        fields = {"grant_type": "authorization_code", "client_id": CLIENT_ID,
                  "redirect_uri": REDIRECT_URI, "code": code,
                  "code_verifier": VERIFIER, "resource": RESOURCE}
        fields.update(changes)
        return self.public.post("/oauth/token", data=fields)

    def test_discovery_challenge_and_pkce_round_trip(self):
        protected = self.public.get("/.well-known/oauth-protected-resource")
        self.assertEqual(protected.json()["resource"], RESOURCE)
        server = self.public.get("/.well-known/oauth-authorization-server")
        self.assertEqual(server.json()["issuer"], ISSUER)
        self.assertEqual(server.json()["code_challenge_methods_supported"], ["S256"])
        unauthorized = self.public.post("/mcp")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertIn("resource_metadata=", unauthorized.headers["www-authenticate"])
        code = self._approve()
        bad = self._exchange(code, code_verifier="x" * 48)
        self.assertEqual(bad.status_code, 400)
        good = self._exchange(code)
        self.assertEqual(good.status_code, 200)
        tokens = good.json()
        self.assertTrue(self.store.validate(tokens["access_token"], RESOURCE))
        self.assertFalse(self.store.validate(tokens["access_token"], ISSUER))
        self.assertFalse(self.store.validate("garbage", RESOURCE))
        self.assertEqual(self._exchange(code).status_code, 400)
        self.assertEqual(len(self.web.get("/api/v2/devstack/mcp/connections").json["connections"]), 1)

        refreshed = self.public.post("/oauth/token", data={
            "grant_type": "refresh_token", "client_id": CLIENT_ID,
            "refresh_token": tokens["refresh_token"], "resource": RESOURCE,
        })
        self.assertEqual(refreshed.status_code, 200)
        self.assertFalse(self.store.validate(tokens["access_token"], RESOURCE))
        new_token = refreshed.json()["access_token"]
        self.assertTrue(self.store.validate(new_token, RESOURCE))
        grants = self.web.get("/api/v2/devstack/mcp/connections").json["connections"]
        self.assertTrue(grants[0]["created_at"])
        self.assertNotIn("last_used", grants[0])
        revoke = self.web.delete("/api/v2/devstack/mcp/connections/" + grants[0]["id"],
                                 headers=self.headers)
        self.assertEqual(revoke.status_code, 200)
        self.assertFalse(self.store.validate(new_token, RESOURCE))
        self.assertEqual(self.web.get("/api/v2/devstack/mcp/connections").json["connections"], [])

    def test_client_redirect_and_resource_are_fixed(self):
        for change in (
            {"client_id": "https://evil.example/client.json"},
            {"redirect_uri": "https://evil.example/"},
            {"resource": "https://evil.example/mcp"},
            {"code_challenge_method": "plain"},
            {"scope": "admin"},
        ):
            with self.subTest(change=change):
                response = self._request(**change)
                self.assertNotIn(response.status_code, (302, 303))
        self.assertEqual(self.web.get("/api/v2/devstack/mcp/connections").json["connections"], [])

    def test_denial_does_not_create_grant(self):
        location = self._request().headers["location"]
        request_id = parse_qs(urlparse(location).query)["oauth_request"][0]
        response = self.web.post(
            "/api/v2/devstack/mcp/auth-requests/" + request_id + "/deny",
            headers=self.headers, json={})
        self.assertEqual(response.status_code, 200)
        redirect = parse_qs(urlparse(response.json["redirect"]).query)
        self.assertEqual(redirect["error"], ["access_denied"])
        self.assertEqual(redirect["iss"], [ISSUER])
        self.assertEqual(self.web.get("/api/v2/devstack/mcp/connections").json["connections"], [])


if __name__ == "__main__":
    unittest.main()
