"""Issue #116: v0.7.11's preflight confused historical and actual route layouts."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from devstack import funnel_preflight as preflight, networking
from devstack.mcp_diagnostics import snapshot

HOST="devstack.example.ts.net"
STATUS={"BackendState":"Running",
        "Self":{"ID":"node-safe","DNSName":HOST+"."},
        "CurrentTailnet":{"Name":"example.ts.net"}}
PIN={"node_id":"node-safe","tailnet":"example.ts.net"}


class FunnelPreflightTests(unittest.TestCase):
    def test_healthy_current_public_root_proxy_and_pinned_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            pin=Path(folder)/"pin.json"
            pin.write_text(json.dumps(PIN))
            calls=[]
            current=networking.expected(HOST,new_private=True,public=True)
            self.assertEqual(current["Web"][HOST+":443"]["Handlers"],
                             {"/":{"Proxy":"http://127.0.0.1:8768"}})
            def read(*args):
                calls.append(args)
                return STATUS if args[1]=="status" else current
            with patch.object(preflight,"_json_status",side_effect=read), patch(
                "devstack.networking.read_preferences",
                return_value={"mcp":True,"webhooks":False}), patch.object(
                preflight,"_gateway_ready",return_value=True):
                result=preflight.capture_preflight(pin)
        self.assertEqual(result["health"],"ready")
        self.assertEqual(result["reason"],"none")
        self.assertEqual(result["identity"],"verified")
        self.assertEqual(result["private_dashboard"],"ready")
        self.assertEqual(result["public_funnel"],"configured")
        self.assertEqual(result["gateway"],"ready")
        self.assertEqual(result["external_reachability"],"not_tested")
        self.assertEqual(calls,[("tailscale","status","--json"),
                               ("tailscale","serve","status","--json")])
        self.assertNotIn("node-safe",str(result))
        self.assertNotIn(HOST,str(result))

    def test_disabled_mcp_stays_disabled_even_with_private_serve(self):
        with tempfile.TemporaryDirectory() as folder:
            pin=Path(folder)/"pin.json"
            pin.write_text(json.dumps(PIN))
            with patch.object(preflight,"_json_status",side_effect=[
                STATUS,networking.expected(HOST,new_private=True)]),patch(
                "devstack.networking.read_preferences",
                return_value={"mcp":False,"webhooks":False}),patch.object(
                preflight,"_gateway_ready") as gateway:
                result=preflight.capture_preflight(pin)
                gateway.assert_not_called()
        self.assertEqual(result["health"],"disabled")
        self.assertEqual(result["reason"],"none")
        self.assertEqual(result["public_funnel"],"disabled")

    def test_unenrolled_missing_pin_and_wrong_identity_are_explicit(self):
        with patch.object(preflight,"_json_status",return_value={"BackendState":"NeedsLogin"}):
            result=preflight.capture_preflight()
        self.assertEqual(result["tailscale"],"disconnected")
        self.assertEqual(result["reason"],"tailscale_disconnected")
        with tempfile.TemporaryDirectory() as folder:
            pin=Path(folder)/"pin.json"
            with patch.object(preflight,"_json_status",side_effect=[
                    STATUS,networking.expected(HOST,new_private=True)]),patch(
                    "devstack.networking.read_preferences",return_value={
                    "mcp":False,"webhooks":False}):
                result=preflight.capture_preflight(pin)
            self.assertEqual(result["identity"],"not_configured")
            pin.write_text(json.dumps({"node_id":"wrong","tailnet":"example.ts.net"}))
            with patch.object(preflight,"_json_status",side_effect=[
                    STATUS,networking.expected(HOST,new_private=True)]),patch(
                    "devstack.networking.read_preferences",return_value={
                    "mcp":False,"webhooks":False}):
                result=preflight.capture_preflight(pin)
            self.assertEqual(result["identity"],"changed")
            self.assertEqual(result["health"],"attention")

    def test_webhook_enabled_uses_one_unified_public_route(self):
        with tempfile.TemporaryDirectory() as folder:
            pin=Path(folder)/"pin.json"
            pin.write_text(json.dumps(PIN))
            with patch.object(preflight,"_json_status",side_effect=[
                STATUS,networking.expected(HOST,new_private=True,public=True)]),patch(
                "devstack.networking.read_preferences",
                return_value={"mcp":True,"webhooks":True}),patch.object(
                preflight,"_gateway_ready",return_value=True):
                result=preflight.capture_preflight(pin)
        self.assertEqual(result["public_funnel"],"configured")
        self.assertEqual(result["health"],"ready")

    def test_pending_public_funnel_and_local_gateway_differ(self):
        with tempfile.TemporaryDirectory() as folder:
            pin=Path(folder)/"pin.json"
            pin.write_text(json.dumps(PIN))
            with patch.object(preflight,"_json_status",side_effect=[
                STATUS,networking.expected(HOST,new_private=True)]),patch(
                "devstack.networking.read_preferences",
                return_value={"mcp":True,"webhooks":True}),patch.object(
                preflight,"_gateway_ready",return_value=True):
                result=preflight.capture_preflight(pin)
        self.assertEqual(result["public_funnel"],"pending")
        self.assertEqual(result["gateway"],"ready")
        self.assertEqual(result["health"],"attention")
        self.assertEqual(result["reason"],"public_route_pending")

    def test_historical_path_layout_not_silently_accepted(self):
        from devstack.tailscale_layout import config
        with tempfile.TemporaryDirectory() as folder:
            pin=Path(folder)/"pin.json"
            pin.write_text(json.dumps(PIN))
            with patch.object(preflight,"_json_status",side_effect=[
                STATUS,config(HOST,private="8443",mcp=True)]),patch(
                "devstack.networking.read_preferences",
                return_value={"mcp":True,"webhooks":False}),patch.object(
                preflight,"_gateway_ready",return_value=True):
                result=preflight.capture_preflight(pin)
        self.assertEqual(result["reason"],"routes_unrecognized")
        self.assertEqual(result["gateway"],"ready")
        self.assertEqual(result["public_funnel"],"unknown")
        self.assertEqual(result["health"],"attention")
        self.assertEqual(result["external_reachability"],"not_tested")

    def test_unavailable_status_reports_safe_reason_not_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            pin=Path(folder)/"pin.json"
            pin.write_text(json.dumps(PIN))
            with patch.object(preflight,"_json_status",side_effect=[
                    STATUS,ValueError("private token secret")]),patch(
                    "devstack.networking.read_preferences",
                    return_value={"mcp":True,"webhooks":False}),patch.object(
                    preflight,"_gateway_ready",return_value=True):
                result=preflight.capture_preflight(pin)
        self.assertEqual(result["reason"],"serve_status_unavailable")
        self.assertEqual(result["gateway"],"ready")
        self.assertNotIn("secret",str(result))

    def test_snapshot_rejects_raw_secrets_stale_and_untrusted_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/"snapshot.json"
            now=time.time_ns()//1000000
            file.write_text(json.dumps({"generated_at":now,"available":True,
                "events":[],"truncated":False,
                "network_preflight":{"health":"ready","tailscale":"connected",
                    "identity":"verified","private_dashboard":"ready",
                    "public_funnel":"configured","gateway":"ready",
                    "reason":"password=secret",
                    "external_reachability":"https://secret","token":"secret"}}))
            safe=snapshot(5,file,include_auxiliary=True)["network_preflight"]
            self.assertEqual(safe["health"],"ready")
            self.assertEqual(safe["external_reachability"],"not_tested")
            self.assertEqual(safe["reason"],"snapshot_unavailable")
            self.assertNotIn("secret",str(safe))
            file.write_text(json.dumps({"generated_at":now-300000,"available":True,
                                        "events":[],"network_preflight":{"health":"ready"}}))
            stale=snapshot(5,file,include_auxiliary=True)["network_preflight"]
            self.assertEqual(stale["health"],"unknown")
            self.assertEqual(stale["reason"],"snapshot_unavailable")


if __name__=="__main__":
    unittest.main()
