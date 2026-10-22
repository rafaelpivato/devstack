"""v0.6.2 diagnostic privilege and source-boundary regression checks."""
from pathlib import Path
import unittest
from unittest.mock import Mock
from devstack.control.daemon import Controller
from devstack.ipc.protocol import ProtocolError

ROOT=Path(__file__).resolve().parents[1]


class DiagnosticsSecurityTests(unittest.TestCase):
    def test_private_systemd_boundary_has_no_log_or_docker_escalation(self):
        web=(ROOT/"deploy/devstack-web-v2.service").read_text()
        controller=(ROOT/"deploy/devstack-control.service").read_text()
        source=(ROOT/"src/devstack/api/http.py").read_text()
        self.assertIn("StateDirectory=devstack-web", web)
        self.assertIn("StateDirectoryMode=0700", web)
        self.assertIn("NoNewPrivileges=true", web)
        self.assertIn("CapabilityBoundingSet=", web)
        self.assertNotIn("SupplementaryGroups=docker",web)
        self.assertIn("SupplementaryGroups=docker",controller)
        self.assertNotIn("journalctl",source)
        self.assertNotIn("shell=True",source)
        self.assertIn('call("diagnostics_snapshot", minutes=minutes)',source)

    def test_unavailable_ring_does_not_hide_recent_runtime_receipts(self):
        controller=object.__new__(Controller)
        controller.diagnostics=Mock()
        controller.diagnostics.snapshot.side_effect=OSError("not readable")
        controller.transitions=Mock()
        controller.transitions.diagnostics.return_value={
            "events":[{"at":12,"source":"runtime","level":"info",
                       "code":"transition-succeeded"}],
            "truncated":False
        }
        controller.registry=Mock()
        controller.registry.database.transaction.return_value.__enter__.return_value.execute.return_value.fetchone.return_value=None
        result=controller.handle({"op":"diagnostics_snapshot","minutes":5})
        self.assertEqual(result["sources"],{"controller":"unavailable","runtime":"available"})
        self.assertEqual(len(result["events"]),1)
        with self.assertRaises(ProtocolError):
            controller.handle({"op":"diagnostics_snapshot","minutes":True})
        controller.transitions.diagnostics.assert_called_once_with(5)

    def test_arbitrary_host_log_access_never_added_to_ipc(self):
        from devstack.ipc.protocol import OPERATIONS
        self.assertIn("diagnostics_snapshot",OPERATIONS)
        for name in ("journal","journalctl","host_logs","shell","docker_exec","exec"):
            self.assertNotIn(name,OPERATIONS)


if __name__=="__main__":
    unittest.main()
