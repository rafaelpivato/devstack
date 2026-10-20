"""Executable v0.5.2 release acceptance traceability guard.

Full tests are run by CI; these checks prevent a release from silently
dropping any accepted AC-01..17 regression scenario or Docker/systemd gate.
They supplement, never replace, actual execution on a clean Ubuntu host.
"""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Every acceptance case has at least one named executable scenario. The
# corresponding scenario must also run in CI's full unittest discovery.
SCENARIOS = {
    "AC-01": ("test_v052_registry.py", "test_two_projects_persist_independently"),
    "AC-02": ("test_v052_settings.py", "test_settings_replacement_cannot_touch_another_project"),
    "AC-03": ("test_v052_api.py", "test_switching_and_browser_selection_are_independent"),
    "AC-04": ("test_v052_control.py", "test_activation_then_switch_and_return_persists_projects"),
    "AC-05": ("test_v052_control.py", "test_two_activation_requests_never_overlap"),
    "AC-06": ("test_v052_control.py", "test_failed_cleanup_never_activates_second_project"),
    "AC-07": ("test_v052_control.py", "test_stale_queued_job_cannot_run_after_reactivation"),
    "AC-08": ("test_v052_adhoc_jobs.py", "test_multiple_commands_drain_before_project_switch"),
    "AC-09": ("test_v052_control.py", "test_crash_restart_recovers_to_inactive_not_old_active"),
    "AC-10": ("test_v052_api.py", "test_unknown_or_wrong_project_requests_do_not_mutate_active"),
    "AC-11": ("test_v052_webhook_ingress.py", "test_same_webhook_route_is_separate_per_project"),
    "AC-12": ("test_v052_source_review.py", "test_conflicting_local_changes_require_specific_overwrite_approval"),
    "AC-13": ("test_v052_api.py", "test_actual_unix_socket_transport_end_to_end"),
    "AC-14": ("test_v052_host_cutover.py", "test_systemd_privilege_and_socket_boundary"),
    "AC-15": ("test_v052_manual_updates.py", "test_existing_release_cannot_restart_active_or_unhealthy_runtime"),
    "AC-16": ("test_v052_project_import.py", "test_import_same_github_template_twice_with_independent_projects"),
    "AC-17": ("test_v052_adhoc_jobs.py", "test_cancel_one_command_keeps_other_commands_and_service_running"),
}
ACTUAL_DOCKER = (
    "Verify v0.5.2 real Docker A-to-B-to-A switching",
    'assert output == "persistent"',
    "jobs.submit_adhoc(",
    "jobs.cancel(a.id, second_long)",
    "engine.switch(b.id)",
    "engine.switch(a.id)",
    "assert webhook(a.id)[0] == 503",
    "assert not backend.owned()",
)
ACTUAL_SYSTEMD = (
    'useradd --system',
    'runuser -u "$web"',
    'systemctl restart devstack-ci-control.service',
    'systemctl start devstack-ci-update-request.path',
    'Manual update systemd path handoff exercised successfully',
)


class ReleaseAcceptanceGateTests(unittest.TestCase):
    def test_each_accepted_ac_has_an_executable_regression(self):
        plan = (ROOT / "docs" / "v0.5.2-acceptance-plan.md").read_text()
        self.assertEqual(
            set(SCENARIOS),
            {"AC-" + str(x).zfill(2) for x in range(1, 18)},
        )
        for label, (filename, name) in SCENARIOS.items():
            with self.subTest(ac=label):
                self.assertIn("| " + label + " |", plan)
                path = ROOT / "tests" / filename
                self.assertTrue(path.is_file(), filename)
                tree = ast.parse(path.read_text())
                tests = {
                    f.name for node in ast.walk(tree)
                    if isinstance(node, ast.ClassDef)
                    for f in node.body
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and f.name.startswith("test_")
                }
                self.assertIn(name, tests, label)

    def test_release_workflow_has_non_optional_unit_docker_and_systemd_gates(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        docker = (ROOT / ".github" / "workflows" / "docker-smoke.yml").read_text()
        systemd = (ROOT / "scripts" / "verify-systemd-ci.sh").read_text()
        self.assertIn("needs: [candidate, docker-verification]", workflow)
        self.assertIn("uses: ./.github/workflows/docker-smoke.yml", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("needs: compose-smoke", docker)
        self.assertIn("runs-on: ubuntu-24.04", docker)
        for assertion in ACTUAL_DOCKER:
            with self.subTest(docker=assertion):
                self.assertIn(assertion, docker)
        for assertion in ACTUAL_SYSTEMD:
            with self.subTest(systemd=assertion):
                self.assertIn(assertion, systemd)

    def test_release_candidate_must_have_evidence_matrix_and_manual_host_gate(self):
        evidence = (ROOT / "docs" / "v0.5.2-acceptance-evidence.md").read_text()
        for label in SCENARIOS:
            self.assertIn("| " + label + " |", evidence)
        self.assertIn("DEFERRED TO 0.5.3 — real published-release clean-host installation", evidence)
        self.assertIn("DEFERRED TO 0.5.3 — private Tailscale device-to-service connectivity", evidence)
        plan = (ROOT / "docs" / "v0.5.2-acceptance-plan.md").read_text()
        adr = (ROOT / "docs" / "adr" / "0002-v052-release-gate-and-v053-validation.md").read_text()
        self.assertIn("ADR-0002", plan)
        self.assertIn("v0.5.3", plan)
        for label in SCENARIOS:
            self.assertIn(label, plan)
        self.assertIn("NOT considered passed", adr)
        self.assertIn("human merge", adr.lower())


if __name__ == "__main__":
    unittest.main()
