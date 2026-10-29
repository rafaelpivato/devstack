"""Issue #101: first-time Funnel diagnostics without changing existing routing."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack import funnel_preflight as preflight
from devstack.mcp_diagnostics import snapshot

HOST="devstack.example.ts.net"
STATUS={"BackendState":"Running",
        "Self":{"ID":"node-safe","DNSName":HOST+"."},
        "CurrentTailnet":{"Name":"example.ts.net"}}
PIN={"node_id":"node-safe","tailnet":"example.ts.net"}


class FunnelPreflightTests(unittest.TestCase):
    def test_healthy_existing_server_does_not_call_mutating_commands(self):
        from devstack.tailscale_layout import config
        with tempfile.TemporaryDirectory() as folder:
            pin=Path(folder)/"pin.json"
            pin.write_text(json.dumps(PIN))
            calls=[]
            def read(*args):
                calls.append(args)
                return STATUS if args[1]=="status" else config(
                    HOST,private="8443",mcp=True)
            with patch.object(preflight,"_json_status",side_effect=read), patch(
                "devstack.networking.read_preferences",
                return_value={"mcp":True,"webhooks":False}), patch.object(
                preflight,"_gateway_ready",return_value=True):
                result=preflight.capture_preflight(pin)
        self.assertEqual(result["health"],"ready")
        self.assertEqual(result["identity"],"verified")
        self.assertEqual(result["public_funnel"],"configured")
        self.assertEqual(result["gateway"],"ready")
        self.assertEqual(result["external_reachability"],"not_tested")
        self.assertEqual(calls,[("tailscale","status","--json"),
                               ("tailscale","serve","status","--json")])
        self.assertNotIn("node-safe",str(result))
        self.assertNotIn(HOST,str(result))

    def test_new_server_off_is_not_claimed_unhealthy(self):
        from devstack.tailscale_layout import config
        with tempfile.TemporaryDirectory() as folder:
            pin=Path(folder)/"pin.json"
            pin.write_text(json.dumps(PIN))
            with patch.object(preflight,"_json_status",side_effect=[
                STATUS,config(HOST,private="8443")]),patch(
                    "devstack.networking.read_preferences",
                    return_value={"mcp":False,"webhooks":False}),patch.object(
                    preflight,"_gateway_ready") as gateway:
                result=preflight.capture_preflight(pin)
                gateway.assert_not_called()
        self.assertEqual(result["health"],"disabled")
        self.assertEqual(result["public_funnel"],"disabled")

    def test_unenrolled_missing_pin_and_wrong_identity_are_explicit(self):
        with patch.object(preflight,"_json_status",return_value={"BackendState":"NeedsLogin"}):
            result=preflight.capture_preflight()
        self.assertEqual(result["tailscale"],"disconnected")
        self.assertEqual(result["health"],"attention")
        with tempfile.TemporaryDirectory() as folder:
            pin=Path(folder)/"pin.json"
            with patch.object(preflight,"_json_status",side_effect=[STATUS,{}]):
                result=preflight.capture_preflight(pin)
            self.assertEqual(result["identity"],"not_configured")
            pin.write_text(json.dumps({"node_id":"wrong","tailnet":"example.ts.net"}))
            with patch.object(preflight,"_json_status",side_effect=[STATUS,{}]):
                result=preflight.capture_preflight(pin)
            self.assertEqual(result["identity"],"changed")

    def test_existing_webhooks_are_not_modified_by_preflight(self):
        from devstack.tailscale_layout import config
        with tempfile.TemporaryDirectory() as folder:
            pin=Path(folder)/"pin.json"
            pin.write_text(json.dumps(PIN))
            with patch.object(preflight,"_json_status",side_effect=[
                STATUS,config(HOST,private="8443",webhooks=True)]),patch(
                    "devstack.networking.read_preferences",
                    return_value={"mcp":True,"webhooks":True}),patch.object(
                    preflight,"_gateway_ready",return_value=True):
                result=preflight.capture_preflight(pin)
        self.assertEqual(result["public_funnel"],"pending")
        self.assertEqual(result["health"],"attention")

    def test_snapshot_rejects_secret_fields_and_staleness(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/"snapshot.json"
            now=time.time_ns()//1000000
            file.write_text(json.dumps({"generated_at":now,"available":True,
                "events":[],"truncated":False,
                "network_preflight":{"health":"ready","tailscale":"connected",
                    "identity":"verified","private_dashboard":"ready",
                    "public_funnel":"configured","gateway":"ready",
                    "external_reachability":"https://secret","token":"secret"}}))
            safe=snapshot(5,file,include_auxiliary=True)["network_preflight"]
            self.assertEqual(safe["health"],"ready")
            self.assertEqual(safe["external_reachability"],"not_tested")
            self.assertNotIn("secret",str(safe))
            file.write_text(json.dumps({"generated_at":now-300000,"available":True,
                                        "events":[],"network_preflight":{"health":"ready"}}))
            stale=snapshot(5,file,include_auxiliary=True)["network_preflight"]
            self.assertEqual(stale["health"],"unknown")


if __name__=="__main__":
    unittest.main()
