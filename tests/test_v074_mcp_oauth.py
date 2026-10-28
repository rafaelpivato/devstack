"""Real single-owner OAuth grant, private approval and public MCP gate tests."""
import base64
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

from starlette.testclient import TestClient
from devstack.oauth import OAuthStore, OAuthError, CLIENT_ID, CALLBACK, SCOPE
from devstack.public_app import create_app

HOST = "devstack.example.ts.net"
RESOURCE = "https://" + HOST + "/mcp"
ISSUER = "https://" + HOST
VERIFIER = "a" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


def params(state="random-state"):
    return {"client_id": CLIENT_ID, "redirect_uri": CALLBACK,
            "response_type": "code", "state": state,
            "code_challenge": CHALLENGE, "code_challenge_method": "S256",
            "resource": RESOURCE, "scope": SCOPE + " offline_access"}


class OAuthSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = OAuthStore(Path(self.temp.name) / "oauth.sqlite3")
        self.store.bootstrap()
        self.addCleanup(self.temp.cleanup)

    def test_pkce_replay_refresh_and_revoke_without_usage_tracking(self):
        identifier = self.store.start(params(), RESOURCE)
        self.assertEqual(self.store.pending(identifier)["client"], "ChatGPT")
        callback = self.store.decide(identifier, True, ISSUER)
        parsed = parse_qs(urlparse(callback).query)
        self.assertEqual(parsed["iss"], [ISSUER])
        self.assertEqual(parsed["state"], ["random-state"])
        code = parsed["code"][0]
        with self.assertRaises(OAuthError):
            self.store.decide(identifier, True, ISSUER)
        with self.assertRaises(OAuthError):
            self.store.exchange({"client_id":CLIENT_ID,"resource":RESOURCE,
                "grant_type":"authorization_code","code":code,
                "redirect_uri":CALLBACK,"code_verifier":"z"*64}, RESOURCE)
        payload = {"client_id": CLIENT_ID,"resource":RESOURCE,"grant_type":"authorization_code",
                   "code":code,"redirect_uri":CALLBACK,"code_verifier":VERIFIER}
        tokens = self.store.exchange(payload, RESOURCE)
        self.assertTrue(self.store.valid(tokens["access_token"], RESOURCE))
        self.assertFalse(self.store.valid(tokens["access_token"], "https://other.example/mcp"))
        self.assertEqual(len(self.store.connections()), 1)
        self.assertRegex(self.store.connections()[0]["created_at"], r"^\d{4}-")
        self.assertNotIn("last_used", self.store.connections()[0])
        with self.assertRaises(OAuthError):
            self.store.exchange(payload, RESOURCE)
        refreshed = self.store.exchange({"client_id": CLIENT_ID,"resource": RESOURCE,
            "grant_type":"refresh_token", "refresh_token": tokens["refresh_token"]}, RESOURCE)
        self.assertFalse(self.store.valid(tokens["access_token"], RESOURCE))
        self.assertTrue(self.store.valid(refreshed["access_token"], RESOURCE))
        with self.assertRaises(OAuthError):
            self.store.exchange({"client_id":CLIENT_ID,"resource":RESOURCE,
                "grant_type":"refresh_token","refresh_token":tokens["refresh_token"]}, RESOURCE)
        self.store.revoke(self.store.connections()[0]["id"])
        self.assertFalse(self.store.valid(refreshed["access_token"], RESOURCE))
        self.assertEqual(self.store.connections(), [])

    def test_only_exact_pinned_chatgpt_client_and_private_approval(self):
        for change in ({"client_id":"attacker"},{"redirect_uri":"https://evil.test"},
                       {"resource":"https://evil.test/mcp"},{"code_challenge_method":"plain"},
                       {"code_challenge":"bad"}):
            with self.subTest(change=change), self.assertRaises(OAuthError):
                self.store.start(params() | change, RESOURCE)
        identifier = self.store.start(params(), RESOURCE)
        rejected = self.store.decide(identifier, False, ISSUER)
        self.assertEqual(parse_qs(urlparse(rejected).query)["error"],["access_denied"])

    def test_http_discovery_flow_and_auth_gate(self):
        with TestClient(create_app(HOST, mcp_enabled=True,
                                   webhooks_enabled=False, oauth_store=self.store),
                        base_url="https://"+HOST) as client:
            blocked = client.post("/mcp", data=b"{}")
            self.assertEqual(blocked.status_code, 401)
            self.assertIn("oauth-protected-resource/mcp", blocked.headers["www-authenticate"])
            self.assertEqual(client.get("/").status_code, 404)
            resource = client.get("/.well-known/oauth-protected-resource").json()
            self.assertEqual(resource["resource"], RESOURCE)
            metadata = client.get("/.well-known/oauth-authorization-server").json()
            self.assertEqual(metadata["issuer"], ISSUER)
            self.assertEqual(metadata["token_endpoint_auth_methods_supported"], ["none"])
            self.assertTrue(metadata["authorization_response_iss_parameter_supported"])
            response = client.get("/oauth/authorize", params=params(), follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers["location"].startswith(
                "https://"+HOST+":8443/administration/mcp/authorize?request_id="))
            request_id = parse_qs(urlparse(response.headers["location"]).query)["request_id"][0]
            code = parse_qs(urlparse(self.store.decide(request_id, True, ISSUER)).query)["code"][0]
            token = client.post("/oauth/token", data={
                "client_id":CLIENT_ID,"grant_type":"authorization_code",
                "code":code,"code_verifier":VERIFIER,"resource":RESOURCE,"redirect_uri":CALLBACK})
            self.assertEqual(token.status_code, 200)
            self.assertIn("refresh_token", token.json())
            self.assertNotEqual(client.post("/mcp", data=b"{}", headers={
                "Authorization":"Bearer "+token.json()["access_token"]}).status_code, 401)
            self.store.revoke(self.store.connections()[0]["id"])
            self.assertEqual(client.post("/mcp", data=b"{}", headers={
                "Authorization":"Bearer "+token.json()["access_token"]}).status_code, 401)


if __name__=="__main__":
    unittest.main()
