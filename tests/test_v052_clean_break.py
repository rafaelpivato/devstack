"""Guard the deliberate v0.5.2 clean break from the legacy singleton."""
import importlib
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "devstack"

OLD_MODULES = (
    "applications", "command_runner", "container_inventory", "core",
    "dashboard_state", "dev_ports", "docker_stacks", "project_setup",
    "project_sync", "runtime", "template_import", "web", "webhooks",
    "workspace",
)
OLD_DEPLOY = ("devstack.service", "devstack-webhooks.service")
OLD_TEMPLATES = (
    "command_output.html", "commands.html", "dashboard.html",
    "docker_output.html", "docker_project.html", "github_settings.html",
    "logs.html", "project_configuration.html", "webhooks.html",
)


class V2OnlyCleanupTests(unittest.TestCase):
    def test_no_standalone_legacy_supervisor_or_v1_web_package(self):
        for name in OLD_MODULES:
            with self.subTest(name=name):
                self.assertFalse((PACKAGE / (name + ".py")).exists())
        for name in OLD_DEPLOY:
            self.assertFalse((ROOT / "deploy" / name).exists())
        for name in OLD_TEMPLATES:
            self.assertFalse((PACKAGE / "templates" / name).exists())

    def test_no_legacy_entry_point(self):
        scripts = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )["project"]["scripts"]
        self.assertNotIn("devstack", scripts)
        self.assertEqual(scripts["devstack-control"],
                         "devstack.control.daemon:main")
        self.assertEqual(scripts["devstack-web-v2"],
                         "devstack.api.http:main")

    def test_v2_dependencies_stay_importable_without_singleton_modules(self):
        for module in (
            "devstack.api.http", "devstack.control.daemon",
            "devstack.control.jobs", "devstack.control.ingress",
            "devstack.control.bulk_repositories",
            "devstack.control.private_ports", "devstack.update_control",
            "devstack.github_ssh",
        ):
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.import_module(module))

    def test_only_v2_static_and_templates_packaged(self):
        self.assertEqual(
            {p.name for p in (PACKAGE / "static" / "js").glob("*.js")},
            set(),
        )
        self.assertEqual(
            {p.name for p in (PACKAGE / "static" / "css").glob("*.css")},
            set(),
        )
        self.assertTrue((PACKAGE / "templates" / "app.html").exists())
        self.assertFalse(list((PACKAGE / "templates").glob("v2_*.html")))
        self.assertFalse((PACKAGE / "templates" / "v06_preview.html").exists())

    def test_runtime_installation_uses_single_controller(self):
        installer = (ROOT / "scripts" / "install.sh").read_text()
        self.assertIn("devstack-control.service", installer)
        self.assertIn("devstack-web-v2.service", installer)
        self.assertNotIn("systemctl enable devstack.service", installer)
        self.assertNotIn("systemctl enable devstack-webhooks.service", installer)


if __name__ == "__main__":
    unittest.main()
