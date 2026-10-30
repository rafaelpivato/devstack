"""Validate Ubuntu bootstrap scripts without touching the host."""
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BootstrapScriptTests(unittest.TestCase):
    def test_bash_syntax(self):
        for name in ("scripts/install.sh", "scripts/enable-tailscale.sh"):
            with self.subTest(name=name):
                subprocess.run(["bash", "-n", str(ROOT / name)], check=True)

    def test_noninteractive_requires_secret_file(self):
        script = (ROOT / "scripts/enable-tailscale.sh").read_text()
        self.assertIn("TAILSCALE_NONINTERACTIVE", script)
        self.assertIn("TAILSCALE_AUTH_KEY_FILE", script)
        self.assertIn("--auth-key=file:", script)
        self.assertIn("stat -c %u", script)
        self.assertIn("-perm /077", script)

    def test_cloud_init_activation_is_bounded(self):
        script = (ROOT / "scripts/enable-tailscale.sh").read_text()
        self.assertIn("TAILSCALE_BROWSER_ACTIVATION", script)
        self.assertIn("tailscale up --timeout=20s", script)

    def test_hostname_option_applies_to_all_initial_login_modes(self):
        script = (ROOT / "scripts/enable-tailscale.sh").read_text()
        self.assertIn('DEVSTACK_TAILSCALE_HOSTNAME', script)
        self.assertIn('"--hostname=$requested_name"', script)
        self.assertIn('tailscale up --json --timeout=20s "${hostname_arg[@]}"', script)
        self.assertIn('tailscale up --timeout=20s "${hostname_arg[@]}"', script)
        self.assertIn('tailscale up "--auth-key=file:$key_file" "${hostname_arg[@]}"', script)
        self.assertIn('tailscale up "${hostname_arg[@]}"', script)

    def test_install_is_opt_in(self):
        script = (ROOT / "scripts/install.sh").read_text()
        self.assertIn('DEVSTACK_TAILSCALE:-0', script)
        self.assertIn('TAILSCALE_BROWSER_ACTIVATION=1', script)
        self.assertIn('TAILSCALE_NONINTERACTIVE=1', script)
        self.assertIn('devstack-tailscale-finish.timer', script)
        self.assertIn('devstack-bootstrap.service', script)
        self.assertIn('DEVSTACK_BOOTSTRAP_SECRET', script)
        self.assertIn('TAILSCALE_LOGIN_JSON_FILE', script)


if __name__ == "__main__":
    unittest.main()
