"""MCP v0.7 hello: isolated state, safe schema, real HTTP protocol."""
import json
import socket
import subprocess
import sys
import time
import unittest
from urllib.request import Request, urlopen

from devstack.mcp_hello import HelloState, public_host, transport_security


class HelloStateTests(unittest.TestCase):
    def test_round_trip_is_ephemeral(self):
        one = HelloState()
        self.assertEqual(one.read(), "world")
        self.assertEqual(one.write("ChatGPT test"), "ChatGPT test")
        self.assertEqual(one.read(), "ChatGPT test")
        self.assertEqual(HelloState().read(), "world")

    def test_reject_unsafe_values(self):
        state = HelloState()
        for value in ("", "x" * 81, "a" + chr(10) + "b", chr(0)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                state.write(value)
        self.assertEqual(state.read(), "world")

    def test_hostname_allowlist(self):
        self.assertEqual(public_host("MyNode.Example.ts.net"), "mynode.example.ts.net")
        for hostname in ("*.ts.net", "bad:10000", "a..b", "-host", "/mcp"):
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                public_host(hostname)
        security = transport_security("mynode.example.ts.net")
        self.assertIn("mynode.example.ts.net:*", security.allowed_hosts)
        self.assertNotIn("*", security.allowed_hosts)


class ProtocolTests(unittest.TestCase):
    """One real SDK server over HTTP; no simulated JSON-RPC dispatcher."""

    def test_remote_shape_and_write_read(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-m", "devstack.mcp_hello", "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        url = f"http://127.0.0.1:{port}/mcp"
        def call(method, params=None, request_id=1):
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            request = Request(url, data=json.dumps(payload).encode(), headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Mcp-Protocol-Version": "2026-07-28",
            })
            with urlopen(request, timeout=4) as response:
                self.assertEqual(response.headers.get_content_type(), "application/json")
                return json.load(response)
        try:
            for _ in range(40):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.15):
                        break
                except OSError:
                    if proc.poll() is not None:
                        self.fail("MCP process exited before listening")
                    time.sleep(.1)
            else:
                self.fail("MCP did not listen")
            tools = call("tools/list")["result"]["tools"]
            self.assertEqual({tool["name"] for tool in tools},
                             {"hello_read", "hello_write"})
            write = call("tools/call", {"name": "hello_write",
                                      "arguments": {"value": "connected"}}, 2)
            self.assertNotIn("error", write)
            read = call("tools/call", {"name": "hello_read", "arguments": {}}, 3)
            self.assertEqual(read["result"]["structuredContent"]["value"], "connected")
            self.assertNotIn("project_id", json.dumps(read))
            forbidden = call("tools/call", {"name": "repository_update",
                                            "arguments": {}}, 4)
            self.assertTrue(forbidden.get("result", {}).get("isError") or
                            "error" in forbidden)
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)
