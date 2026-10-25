"""MCP v0.7 hello: isolated state, safe schema, actual SDK over HTTP."""
import asyncio
import socket
import subprocess
import sys
import time
import unittest

from mcp import Client
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
    """Real SDK client/server handshake and calls over Streamable HTTP."""

    def test_remote_shape_and_write_read(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-m", "devstack.mcp_hello", "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            for _ in range(60):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.15):
                        break
                except OSError:
                    if proc.poll() is not None:
                        self.fail("MCP process exited before listening")
                    time.sleep(.1)
            else:
                self.fail("MCP did not listen")

            async def exercise():
                async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                    tools = (await client.list_tools()).tools
                    self.assertEqual({tool.name for tool in tools},
                                     {"hello_read", "hello_write"})
                    changed = await client.call_tool(
                        "hello_write", {"value": "connected"})
                    self.assertFalse(changed.is_error)
                    result = await client.call_tool("hello_read", {})
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content["value"], "connected")
                    self.assertNotIn("project_id", str(result.structured_content))

            asyncio.run(exercise())
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)
