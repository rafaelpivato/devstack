"""MCP integration: direct controller Unix IPC, with no Web HTTP service."""
import asyncio
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from mcp import Client

from devstack.control.daemon import ControlServer
from devstack.mcp_gateway import (
    PingState, probe_controller, public_host, transport_security,
)


class PingStateTests(unittest.TestCase):
    def test_round_trip_is_ephemeral(self):
        first = PingState()
        self.assertIsNone(first.read())
        self.assertEqual(first.ping("ChatGPT test"), "ChatGPT test")
        self.assertEqual(first.read(), "ChatGPT test")
        self.assertIsNone(PingState().read())

    def test_reject_unsafe_values(self):
        state = PingState()
        for value in ("", "x" * 81, "a" + chr(10) + "b", chr(0)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                state.ping(value)
        self.assertIsNone(state.read())

    def test_hostname_allowlist(self):
        self.assertEqual(public_host("MyNode.Example.ts.net"), "mynode.example.ts.net")
        for hostname in ("*.ts.net", "bad:10000", "a..b", "-host", "/mcp"):
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                public_host(hostname)
        security = transport_security("mynode.example.ts.net")
        self.assertIn("mynode.example.ts.net:*", security.allowed_hosts)
        self.assertNotIn("*", security.allowed_hosts)

    def test_unreachable_controller_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(probe_controller(Path(folder) / "absent.sock"), "unavailable")


class FakeController:
    """Only the existing controller health operation is available here."""

    def handle(self, request):
        if request != {"op": "health"}:
            raise AssertionError("Unexpected privileged controller operation")
        return {"alive": True, "runtime": "active"}


class ProtocolTests(unittest.TestCase):
    def test_live_mcp_direct_unix_ipc_without_web(self):
        with tempfile.TemporaryDirectory() as folder:
            socket_path = Path(folder) / "control.sock"
            with ControlServer(socket_path, FakeController()) as controller:
                ipc_thread = threading.Thread(target=controller.serve_forever, daemon=True)
                ipc_thread.start()
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    mcp_port = sock.getsockname()[1]
                proc = subprocess.Popen(
                    [sys.executable, "-m", "devstack.mcp_gateway",
                     "--port", str(mcp_port),
                     "--socket", str(socket_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
                try:
                    for _ in range(60):
                        try:
                            with socket.create_connection(("127.0.0.1", mcp_port), timeout=.15):
                                break
                        except OSError:
                            if proc.poll() is not None:
                                self.fail("MCP process exited before listening")
                            time.sleep(.1)
                    else:
                        self.fail("MCP did not listen")

                    async def exercise():
                        async with Client(f"http://127.0.0.1:{mcp_port}/mcp") as client:
                            names = {t.name for t in (await client.list_tools()).tools}
                            self.assertEqual(names, {
                                "devstack_status", "devstack_ping",
                                "devstack_projects", "devstack_runtime", "devstack_project",
                                "devstack_repository_status", "devstack_services",
                                "devstack_service_logs", "devstack_jobs",
                                "devstack_job_logs", "devstack_diagnostics",
                                "devstack_transition", "devstack_transition_status",
                                "devstack_repository_check", "devstack_repository_update",
                                "devstack_service_action", "devstack_task_run",
                                "devstack_job", "devstack_job_cancel",
                                "devstack_repository_sync", "devstack_repository_sync_policy",
                                "devstack_repository_reactions", "devstack_repository_reaction_policy",
                                "devstack_repository_update_all", "devstack_repository_update_all_status",
                                "devstack_repository_add", "devstack_repository_add_status",
                                "devstack_branch_preview", "devstack_branch_apply",
                                "devstack_project_create", "devstack_project_import",
                                "devstack_project_import_status", "devstack_project_rename",
                                "devstack_compose_config", "devstack_compose_save",
                                "devstack_project_settings", "devstack_project_settings_save",
                                "devstack_source_review", "devstack_source_apply",
                                "devstack_webhooks", "devstack_webhook_upsert",
                                "devstack_webhook_remove", "devstack_compose_action",
                                "devstack_adhoc", "devstack_private_ports",
                            })
                            first = await client.call_tool("devstack_status", {})
                            self.assertFalse(first.is_error)
                            self.assertEqual(first.structured_content["controller"], "connected")
                            self.assertEqual(first.structured_content["version"], __import__("importlib.metadata", fromlist=["version"]).version("devstack"))
                            self.assertIsNone(first.structured_content["last_ping"])
                            ping = await client.call_tool("devstack_ping", {"value": "connected"})
                            self.assertFalse(ping.is_error)
                            self.assertEqual(ping.structured_content["pong"], "connected")
                            status = await client.call_tool("devstack_status", {})
                            self.assertEqual(status.structured_content["last_ping"], "connected")
                            self.assertNotIn("project_id", str(status.structured_content))
                            # Emulate controller loss with web still running.
                            controller.shutdown()
                            ipc_thread.join(3)
                            controller.server_close()
                            disconnected = await client.call_tool("devstack_status", {})
                            self.assertEqual(
                                disconnected.structured_content["controller"], "unavailable"
                            )
                            self.assertEqual(
                                disconnected.structured_content["last_ping"], "connected"
                            )

                    asyncio.run(exercise())
                finally:
                    proc.terminate()
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate(timeout=5)
                    controller.shutdown()
                    ipc_thread.join(3)
