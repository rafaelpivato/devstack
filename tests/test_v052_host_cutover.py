"""v0.5.2 clean Ubuntu topology policy: no singleton install or Docker web user.

Static policy tests supplement, but do not replace, fresh-host systemd,
Docker and private Tailscale acceptance checks.
"""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install.sh"
CONTROL = ROOT / "deploy" / "devstack-control.service"
WEB = ROOT / "deploy" / "devstack-web-v2.service"
BOOTSTRAP = ROOT / "deploy" / "devstack-bootstrap.service"


class FreshInstallPolicy(unittest.TestCase):
    def test_bash_installer_is_syntax_valid(self):
        subprocess.run(["bash", "-n", str(INSTALL)], check=True)

    def test_installer_rejects_singleton_and_legacy_schemas(self):
        code = INSTALL.read_text()
        for fragment in (
            "-e /etc/systemd/system/devstack.service",
            "-e /var/lib/devstack/control.sqlite3",
            "-e /opt/devstack/current",
            "requires a clean host",
            'if numbers < (0, 5, 2):',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, code)
        self.assertNotIn('usermod -aG docker "$web_user"', code)
        self.assertIn('usermod -aG docker "$control_user"', code)
        self.assertIn('"$socket_group" "$web_user"', code)

    def test_install_downloads_and_starts_both_services_only(self):
        code = INSTALL.read_text()
        self.assertIn("deploy/devstack-control.service", code)
        self.assertIn("deploy/devstack-web-v2.service", code)
        self.assertIn("systemctl enable devstack-control.service devstack-web-v2.service", code)
        self.assertNotIn("systemctl enable devstack.service", code)
        self.assertNotIn("systemctl enable devstack-webhooks.service", code)
        self.assertNotIn('devstack.service" > /etc/systemd/system', code)
        self.assertIn('chown root:"$socket_group" /etc/devstack/app.env', code)
        self.assertIn('chmod 0640 /etc/devstack/app.env', code)
        self.assertIn('install -d -o "$control_user" -g "$control_user" -m 0700 /var/lib/devstack', code)

    def test_systemd_privilege_and_socket_boundary(self):
        web = WEB.read_text()
        ctl = CONTROL.read_text()
        self.assertIn("User=@DEVSTACK_WEB_USER@", web)
        self.assertIn("Group=@DEVSTACK_SOCKET_GROUP@", web)
        self.assertIn("Requires=devstack-control.service", web)
        self.assertNotIn("SupplementaryGroups=docker", web)
        self.assertNotIn("devstack.web:creat", web)
        self.assertIn("devstack.api.http:create_app()", web)
        self.assertIn("127.0.0.1:8765", web)
        self.assertIn("User=@DEVSTACK_CONTROL_USER@", ctl)
        self.assertIn("SupplementaryGroups=docker", ctl)
        self.assertIn("RuntimeDirectoryMode=0750", ctl)
        self.assertIn("UMask=0007", ctl)
        self.assertIn("devstack-control --state /var/lib/devstack", ctl)
        self.assertIn("ProtectSystem=strict", web)
        self.assertIn("ProtectSystem=strict", ctl)
        self.assertIn("Group=@DEVSTACK_WEB_USER@", BOOTSTRAP.read_text())

    def test_updater_will_not_accept_singleton_health(self):
        from scripts import updater
        from unittest.mock import patch
        with patch("scripts.updater.healthy", return_value=False):
            with self.assertRaises(ValueError):
                updater.activate(
                    "v0.5.1", "rafaelpivato/devstack",
                    Path("/tmp/devstack-test-incompatible"),
                    Path("/tmp/devstack-state-test-incompatible"),
                )


if __name__ == "__main__":
    unittest.main()
