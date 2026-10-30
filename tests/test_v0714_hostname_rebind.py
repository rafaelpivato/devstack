"""v0.7.14: recover old-FQDN Serve/Funnel configuration without SSH.

All subprocesses are mocked: tests exercise exact node-level map rewrites,
first-boot/updater timer recovery, idempotency and fail-closed safety.
"""
from contextlib import nullcontext
import json
import subprocess
import unittest
from unittest.mock import patch

from devstack import networking
from devstack.mcp_management import MCPManagementError

OLD = "ubuntu-1.tailnet.ts.net"
NEW = "devstack.tailnet.ts.net"
PREFS_PUBLIC = {"mcp": True, "webhooks": False}
PREFS_PRIVATE = {"mcp": False, "webhooks": False}


class RebindTests(unittest.TestCase):
    def _repair(self, *, public, prefs, old=OLD, new=NEW):
        before = networking.expected(old, new_private=True, public=public)
        after = networking.expected(new, new_private=True, public=public)
        with patch.object(networking, "json_command", side_effect=[before, before, after]), \
             patch.object(networking, "pinned_hostname", return_value=new), \
             patch.object(networking, "read_preferences", return_value=prefs), \
             patch.object(networking, "log_event") as event, \
             patch.object(networking.subprocess, "run") as run:
            self.assertTrue(networking.repair_renamed_hostname(new))
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["tailscale", "serve", "set-raw"])
        self.assertEqual(kwargs["timeout"], 25)
        self.assertTrue(kwargs["check"])
        self.assertTrue(kwargs["capture_output"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(json.loads(kwargs["input"]), after)
        self.assertEqual(after["Web"][new+":8443"],
                         {"Handlers": {"/": {"Proxy": networking.PRIVATE}}})
        self.assertNotIn(new+":8443", after["AllowFunnel"])
        self.assertEqual(after["AllowFunnel"].get(new+":443"), True if public else None)
        self.assertEqual([x.args[0] for x in event.call_args_list],
                         ["hostname_rebind_requested", "hostname_rebind_succeeded"])

    def test_automatic_rebind_with_public_mcp(self):
        self._repair(public=True, prefs=PREFS_PUBLIC)

    def test_automatic_rebind_private_only(self):
        self._repair(public=False, prefs=PREFS_PRIVATE)

    def test_healthy_configuration_is_noop(self):
        good = networking.expected(NEW, new_private=True, public=True)
        with patch.object(networking, "json_command", return_value=good), \
             patch.object(networking.subprocess, "run") as run:
            self.assertFalse(networking.repair_renamed_hostname(NEW))
        run.assert_not_called()

    def test_reject_unknown_routes_and_public_dashboard(self):
        base = networking.expected(OLD, new_private=True, public=True)
        variants = []
        unknown = json.loads(json.dumps(base))
        unknown["Web"][OLD+":443"]["Handlers"]["/"]["Proxy"] = networking.PRIVATE
        variants.append(unknown)
        public_private = json.loads(json.dumps(base))
        public_private["AllowFunnel"][OLD+":8443"] = True
        variants.append(public_private)
        other = json.loads(json.dumps(base))
        other["Web"][OLD+":443"]["Handlers"]["/admin"] = {"Proxy": "http://127.0.0.1:9999"}
        variants.append(other)
        other_host = json.loads(json.dumps(base))
        other_host["Web"]["foreign.tailnet.ts.net:8443"] = {"Handlers": {"/": {"Proxy": networking.PRIVATE}}}
        variants.append(other_host)
        extra = json.loads(json.dumps(base))
        extra["TCP"]["9000"] = {"HTTPS": True}
        variants.append(extra)
        for raw in variants:
            with self.subTest(raw=raw), \
                 patch.object(networking, "json_command", return_value=raw), \
                 patch.object(networking.subprocess, "run") as run:
                with self.assertRaises(MCPManagementError):
                    networking.repair_renamed_hostname(NEW)
                run.assert_not_called()

    def test_refuse_different_tailnet_and_old_untrusted_hostname(self):
        raw = networking.expected(OLD, new_private=True)
        for dest in ("devstack.another.ts.net", "devstack.evil.example"):
            with self.subTest(dest=dest), \
                 patch.object(networking, "json_command", return_value=raw), \
                 patch.object(networking.subprocess, "run") as run:
                with self.assertRaises(MCPManagementError):
                    networking.repair_renamed_hostname(dest)
                run.assert_not_called()

    def test_refuse_unexpected_public_intent_without_writing(self):
        for public,prefs in [(True, PREFS_PRIVATE), (False, PREFS_PUBLIC)]:
            raw = networking.expected(OLD, new_private=True, public=public)
            with self.subTest(public=public), \
                 patch.object(networking, "json_command", return_value=raw), \
                 patch.object(networking, "read_preferences", return_value=prefs), \
                 patch.object(networking.subprocess, "run") as run:
                with self.assertRaises(MCPManagementError) as err:
                    networking.repair_renamed_hostname(NEW)
                self.assertEqual(err.exception.code, "hostname_rebind_intent_mismatch")
                run.assert_not_called()

    def test_refuse_mismatched_identity_before_writing(self):
        raw = networking.expected(OLD, new_private=True)
        with patch.object(networking, "json_command", return_value=raw), \
             patch.object(networking, "read_preferences", return_value=PREFS_PRIVATE), \
             patch.object(networking, "pinned_hostname", return_value=OLD), \
             patch.object(networking.subprocess, "run") as run:
            with self.assertRaises(MCPManagementError) as err:
                networking.repair_renamed_hostname(NEW)
        self.assertEqual(err.exception.code, "hostname_rebind_changed")
        run.assert_not_called()

    def test_refuse_concurrent_route_change_before_writing(self):
        old = networking.expected(OLD, new_private=True)
        changed = networking.expected(OLD, new_private=True, public=True)
        with patch.object(networking, "json_command", side_effect=[old, changed]), \
             patch.object(networking, "read_preferences", return_value=PREFS_PRIVATE), \
             patch.object(networking, "pinned_hostname", return_value=NEW), \
             patch.object(networking.subprocess, "run") as run:
            with self.assertRaises(MCPManagementError) as err:
                networking.repair_renamed_hostname(NEW)
        self.assertEqual(err.exception.code, "hostname_rebind_changed")
        run.assert_not_called()

    def test_failed_cli_stays_fail_closed_and_safe(self):
        old = networking.expected(OLD, new_private=True)
        for exc in (subprocess.TimeoutExpired(["tailscale", "serve", "set-raw"], 25),
                    subprocess.CalledProcessError(1, ["tailscale", "serve", "set-raw"],
                                                 stderr="private-password")):
            with self.subTest(exc=type(exc).__name__), \
                 patch.object(networking, "json_command", side_effect=[old, old]), \
                 patch.object(networking, "pinned_hostname", return_value=NEW), \
                 patch.object(networking, "read_preferences", return_value=PREFS_PRIVATE), \
                 patch.object(networking.subprocess, "run", side_effect=exc):
                with self.assertRaises(MCPManagementError) as error:
                    networking.repair_renamed_hostname(NEW)
                self.assertEqual(error.exception.code, "hostname_rebind_command_failed")
                self.assertNotIn("password", str(error.exception))

    def test_bad_readback_does_not_claim_success(self):
        old = networking.expected(OLD, new_private=True)
        with patch.object(networking, "json_command", side_effect=[old, old, old]), \
             patch.object(networking, "pinned_hostname", return_value=NEW), \
             patch.object(networking, "read_preferences", return_value=PREFS_PRIVATE), \
             patch.object(networking.subprocess, "run"):
            with self.assertRaises(MCPManagementError) as error:
                networking.repair_renamed_hostname(NEW)
        self.assertEqual(error.exception.code, "hostname_rebind_unverified")


class UpdaterRecoveryTests(unittest.TestCase):
    def test_root_network_timer_repairs_without_dashboard_and_retries_safely(self):
        with patch.object(networking, "network_lock", return_value=nullcontext()), \
             patch.object(networking, "pinned_hostname", return_value=NEW), \
             patch.object(networking, "state", side_effect=[
                 MCPManagementError("unknown_tailscale_routes"),
                 ("new", False, False)]), \
             patch.object(networking, "repair_renamed_hostname", return_value=True) as repair, \
             patch.object(networking, "read_preferences", return_value=PREFS_PUBLIC), \
             patch.object(networking, "_ready_public") as public, \
             patch.object(networking, "_finish_bootstrap"), \
             patch.object(networking, "run_command") as cmd, \
             patch.object(networking, "log_event"):
            self.assertEqual(networking.reconcile(), "network_reconciled")
        repair.assert_called_once_with(NEW)
        public.assert_called_once_with(NEW, PREFS_PUBLIC)
        self.assertNotIn(("tailscale", "serve", "reset"), [x.args for x in cmd.call_args_list])
        self.assertNotIn(("tailscale", "funnel", "reset"), [x.args for x in cmd.call_args_list])

    def test_unknown_routes_no_automatic_reset(self):
        with patch.object(networking, "network_lock", return_value=nullcontext()), \
             patch.object(networking, "pinned_hostname", return_value=NEW), \
             patch.object(networking, "state", side_effect=MCPManagementError("unknown_tailscale_routes")), \
             patch.object(networking, "repair_renamed_hostname",
                          side_effect=MCPManagementError("hostname_rebind_unrecognized")), \
             patch.object(networking, "run_command") as cmd:
            with self.assertRaises(MCPManagementError):
                networking.reconcile()
            cmd.assert_not_called()

    def test_existing_release_hook_owns_timer(self):
        from devstack import networking
        self.assertIn("devstack-network-reconcile.timer", networking.TIMER)
        self.assertIn("devstack.networking --reconcile", networking.RECONCILE_UNIT)
