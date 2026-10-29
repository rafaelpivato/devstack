"""v0.7.10 shared diagnostic coverage without private Web HTTP access."""
from pathlib import Path
from unittest.mock import patch
import json
import sqlite3
import tempfile
import time
import unittest

from devstack.diagnostics import DiagnosticStore
from devstack.diagnostics_bridge import capture_auxiliary
from devstack.mcp_diagnostics import snapshot
from devstack.mcp_reads import MCPReadClient
from devstack.control.daemon import Controller
from unittest.mock import Mock


class SharedDiagnosticsTests(unittest.TestCase):
    def test_web_ring_and_updater_are_bounded_and_allowlisted(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            primary=root/"web.sqlite3"
            db=DiagnosticStore(primary)
            db.record("web","web-started")
            db.record("web","web-mcp-enable-requested")
            # Valid SQLite but unrecognized text must not be exposed, even
            # when a privileged root collector reads the file.
            with sqlite3.connect(primary) as con:
                con.execute("INSERT INTO events(at_ns,source,level,code) VALUES(?,?,?,?)",
                            (time.time_ns(),"web","error","Bearer sensitive-secret"))
            username=__import__("pwd").getpwuid(os.geteuid()).pw_name
            with patch("devstack.diagnostics_bridge.update_state",return_value={
                    "phase":"failed","unit_state":"failed","last_result":"exit-code",
                    "last_exit_code":"1"}), patch(
                    "devstack.diagnostics_bridge.updater_service_events",return_value={
                    "events":[{"at":time.time_ns()//1000000,"source":"updater",
                               "level":"error","code":"updater-failed"},
                              {"at":time.time_ns()//1000000,"source":"updater",
                               "level":"info","code":"evil secret payload"}],
                    "available":True}):
                auxiliary=capture_auxiliary(primary=primary,
                    fallback=root/"missing.sqlite3",web_user=username)
            self.assertEqual(auxiliary["web"]["status"],"available")
            self.assertEqual(auxiliary["web"]["storage"]["mode"],"persistent")
            self.assertEqual(len(auxiliary["web"]["events"]),2)
            self.assertNotIn("sensitive-secret",str(auxiliary))
            self.assertEqual(auxiliary["updater"]["status"],"available")
            self.assertEqual([e["code"] for e in auxiliary["updater"]["events"]],
                             ["updater-failed"])

    def test_fallback_and_stale_sources_are_explicit(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            fallback=root/"fallback.sqlite3"
            DiagnosticStore(fallback).record("web","web-started")
            username=__import__("pwd").getpwuid(os.geteuid()).pw_name
            with patch("devstack.diagnostics_bridge.update_state",return_value={
                    "phase":"idle","unit_state":"inactive","last_result":"success",
                    "last_exit_code":"0"}), patch(
                    "devstack.diagnostics_bridge.updater_service_events",return_value={
                    "events":[],"available":False}):
                auxiliary=capture_auxiliary(primary=root/"absent"/"events.sqlite3",
                                            fallback=fallback,web_user=username)
            self.assertEqual(auxiliary["web"]["status"],"partial (volatile fallback)")
            self.assertEqual(auxiliary["web"]["storage"]["mode"],"volatile")
            self.assertEqual(auxiliary["updater"]["status"],"partial (status only)")
            path=root/"snapshot.json"
            path.write_text(json.dumps({"generated_at":int(time.time()*1000)-300000,
                "available":True,"events":[],"auxiliary":auxiliary}))
            stale=snapshot(5,path,include_auxiliary=True)
            self.assertIsNone(stale["generated_at"])
            self.assertEqual(stale["auxiliary"]["web"]["status"],"unavailable")
            self.assertEqual(stale["auxiliary"]["updater"]["status"],"unavailable")

    def test_no_malicious_fields_survive_bounded_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"support.json"
            now=int(time.time()*1000)
            path.write_text(json.dumps({"generated_at":now,"available":True,
                "events":[],"truncated":False,"auxiliary":{
                "web":{"status":"available","storage":{"mode":"persistent",
                    "error_code":"password=abc"},"events":[
                    {"at":now,"source":"web","level":"info","code":"web-started",
                     "extra":"secret"}],"extra":"secret"},
                "updater":{"status":"available","updater_status":{
                    "phase":"idle","unit_state":"inactive","last_result":"success",
                    "last_exit_code":"0"},"events":[]}}}))
            result=snapshot(5,path,include_auxiliary=True)
            self.assertEqual(result["auxiliary"]["web"]["storage"]["error_code"],None)
            self.assertEqual(result["auxiliary"]["web"]["events"][0],
                {"at":now,"source":"web","level":"info","code":"web-started"})
            self.assertNotIn("secret",str(result))
            self.assertNotIn("password",str(result))

    def test_controller_ipc_includes_auxiliary_but_web_keeps_own_live_sources(self):
        now=int(time.time()*1000)
        control=object.__new__(Controller)
        control.diagnostics=Mock()
        control.diagnostics.snapshot.return_value={"events":[],"truncated":False}
        control.transitions=Mock()
        control.transitions.diagnostics.return_value={"events":[],"truncated":False}
        control.registry=Mock()
        with patch("devstack.mcp_diagnostics.snapshot",return_value={
            "available":True,"events":[],"truncated":False,
            "generated_at":now,"auxiliary":{
                "web":{"status":"available","storage":{
                    "mode":"persistent","error_code":None},"events":[{
                    "at":now,"source":"web","level":"info","code":"web-started"}],
                    "truncated":False},
                "updater":{"status":"available","updater_status":{
                    "phase":"idle","unit_state":"inactive","last_result":"success",
                    "last_exit_code":"0"},"events":[],"truncated":False}}}):
            result=control.handle({"op":"diagnostics_snapshot","minutes":5})
        self.assertEqual(result["sources"]["web"],"available")
        self.assertEqual(result["sources"]["updater"],"available")
        self.assertEqual(result["sources"]["mcp"],"available")
        self.assertEqual(result["web_storage"]["mode"],"persistent")
        self.assertEqual(result["events"][0]["code"],"web-started")
        client=MCPReadClient(transport=lambda op,**kw: result)
        values=client.diagnostics(5)
        self.assertEqual(values["sources"]["web"],"available")
        self.assertFalse(values["partial"])
        self.assertEqual(values["updater_status"]["phase"],"idle")
        self.assertEqual(values["support_captured_at"],now)


if __name__=="__main__":
    unittest.main()
