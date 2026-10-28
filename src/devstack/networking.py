"""One root-owned desired-state reconciler for Tailscale Serve/Funnel.

No daemon and no custom reverse proxy: existing root updater and a systemd
oneshot timer call this module. Exact complete route maps only. Neither an
unknown handler nor the private dashboard can be published via Funnel.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import tempfile

from devstack.mcp_management import (MCPManagementError, pinned_hostname,
                                      hostname_from_status, json_command,
                                      run_command, log_event)

# Persistent root-owned opt-in flags contain no secrets. Place under /etc:
# /var/lib/devstack is 0700 (controller-owned) and cannot be read by Web.
PREFS = Path("/etc/devstack-public-ingress.json")
LOCK_FILE = Path("/run/devstack-network-reconcile.lock")
ENV_FILE = Path("/etc/devstack-public.env")
PRIVATE = "http://127.0.0.1:8765"
PUBLIC = "http://127.0.0.1:8768"
WEBHOOK_OLD = "http://127.0.0.1:8767"
MCP_OLD = "http://127.0.0.1:8768"
UNIT = "/etc/systemd/system/devstack-public.service"
RECONCILER = "/etc/systemd/system/devstack-network-reconcile.service"
TIMER = "/etc/systemd/system/devstack-network-reconcile.timer"
DROPIN = "/etc/systemd/system/devstack-control.service.d/10-public-ingress.conf"
WEB_OAUTH_DROPIN = "/etc/systemd/system/devstack-web-v2.service.d/20-mcp-oauth.conf"
OAUTH_DIR = Path("/var/lib/devstack-oauth")

PUBLIC_UNIT = """# Managed by DevStack v0.7+
[Unit]
Description=DevStack unified unprivileged public ingress
After=network.target devstack-control.service
Requires=devstack-control.service
[Service]
Type=simple
DynamicUser=yes
SupplementaryGroups=devstackpublic
EnvironmentFile=-/etc/devstack-public.env
ExecStart=/opt/devstack/current/venv/bin/python -m devstack.public_app
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/devstack-oauth
PrivateTmp=yes
PrivateDevices=yes
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077
[Install]
WantedBy=multi-user.target
"""
RECONCILE_UNIT = """# Managed by DevStack v0.7+
[Unit]
Description=Reconcile DevStack private and public Tailscale ingress
After=network-online.target tailscaled.service devstack-control.service
Wants=network-online.target
[Service]
Type=oneshot
User=root
ExecStart=/opt/devstack/current/venv/bin/python -m devstack.networking --reconcile
TimeoutStartSec=240
"""
RECONCILE_TIMER = """# Managed by DevStack v0.7+
[Unit]
Description=Retry bounded DevStack Tailscale ingress reconciliation
[Timer]
OnBootSec=75s
OnUnitInactiveSec=3min
Persistent=true
Unit=devstack-network-reconcile.service
[Install]
WantedBy=timers.target
"""


def expected(host: str, *, old_private=False, old_hooks=False, old_mcp=False,
             new_private=False, public=False) -> dict:
    tcp, web, funnel = {}, {}, {}
    def add(port, proxy, *, exposed=False):
        key = host + ":" + str(port)
        tcp[str(port)] = {"HTTPS": True}
        web[key] = {"Handlers": {"/": {"Proxy": proxy}}}
        if exposed:
            funnel[key] = True
    if old_private:
        add(443, PRIVATE)
    if old_hooks:
        add(8443, WEBHOOK_OLD, exposed=True)
    if new_private:
        add(8443, PRIVATE)
    if old_mcp:
        add(10000, MCP_OLD, exposed=True)
    if public:
        add(443, PUBLIC, exposed=True)
    return {"TCP": tcp, "Web": web, "AllowFunnel": funnel}


def classify(host: str, raw: dict):
    if not isinstance(raw, dict) or set(raw) - {"TCP", "Web", "AllowFunnel"}:
        raise MCPManagementError("unknown_tailscale_routes")
    actual = {key: raw.get(key) or {} for key in ("TCP", "Web", "AllowFunnel")}
    if any(not isinstance(v, dict) for v in actual.values()):
        raise MCPManagementError("unknown_tailscale_routes")
    if actual == expected(host):
        return ("empty", False, False)
    for old_mcp in (False, True):
        for hooks in (False, True):
            if actual == expected(host, old_private=True, old_hooks=hooks, old_mcp=old_mcp):
                return ("legacy", hooks, old_mcp)
        if actual == expected(host, old_private=True, new_private=True, old_mcp=old_mcp):
            return ("dual_private", False, old_mcp)
        if actual == expected(host, new_private=True, old_mcp=old_mcp):
            return ("private_ready", False, old_mcp)
        if actual == expected(host, new_private=True, public=True, old_mcp=old_mcp):
            return ("new" if not old_mcp else "public_ready", False, old_mcp)
    raise MCPManagementError("unknown_tailscale_routes")


def state(host):
    return classify(host, json_command("tailscale", "serve", "status", "--json"))


def read_preferences() -> dict | None:
    if PREFS.is_symlink():
        raise MCPManagementError("preferences_unsafe")
    try:
        value = json.loads(PREFS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise MCPManagementError("preferences_invalid")
    if (not isinstance(value, dict) or set(value) != {"mcp", "webhooks"}
            or any(type(v) is not bool for v in value.values())):
        raise MCPManagementError("preferences_invalid")
    return value


def _atomic(path: Path, content: str, mode=0o644):
    if path.is_symlink():
        raise MCPManagementError("unsafe_network_file")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
            prefix=".devstack-network-", dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())
    os.replace(temp, path)


def write_preferences(settings: dict):
    if set(settings) != {"mcp", "webhooks"} or any(type(v) is not bool for v in settings.values()):
        raise MCPManagementError("preferences_invalid")
    # Public/private enablement bits contain no credentials; Web needs read-only
    # visibility without access to Tailscale's root-only identity pin.
    _atomic(PREFS, json.dumps(settings, separators=(",", ":")) + "\n", 0o644)


def prepare():
    """One-time unit/group installation BEFORE new controller is started."""
    if os.geteuid() != 0:
        raise MCPManagementError("root_required")
    try:
        import grp
        grp.getgrnam("devstackpublic")
    except KeyError:
        run_command("groupadd", "--system", "devstackpublic", timeout=15)
    _atomic(Path(DROPIN),
            "# Managed by DevStack v0.7+\n[Service]\nSupplementaryGroups=docker devstackpublic\n")
    # Private owner approval and public token verification share only this
    # bounded store, never the Docker/controller administration socket.
    _atomic(Path(WEB_OAUTH_DROPIN),
            "# Managed by DevStack v0.7+\n[Service]\n"
            "SupplementaryGroups=devstackpublic\n"
            "ReadWritePaths=/var/lib/devstack-oauth\n")
    import grp
    gid = grp.getgrnam("devstackpublic").gr_gid
    if OAUTH_DIR.is_symlink():
        raise MCPManagementError("oauth_directory_unsafe")
    OAUTH_DIR.mkdir(parents=True, exist_ok=True, mode=0o2770)
    os.chown(OAUTH_DIR, 0, gid)
    os.chmod(OAUTH_DIR, 0o2770)
    from devstack.oauth_store import _new_state
    for path, contents in (
        (OAUTH_DIR / "state.json", json.dumps(_new_state()) + "\n"),
        (OAUTH_DIR / "state.lock", ""),
    ):
        if path.is_symlink():
            raise MCPManagementError("oauth_file_unsafe")
        if not path.exists():
            _atomic(path, contents, 0o660)
        if not path.is_file() or path.stat().st_gid != gid:
            raise MCPManagementError("oauth_file_unsafe")
        os.chown(path, 0, gid)
        os.chmod(path, 0o660)
    for name, body in (
            (UNIT, PUBLIC_UNIT), (RECONCILER, RECONCILE_UNIT),
            (TIMER, RECONCILE_TIMER)):
        target = Path(name)
        if target.exists() and not target.read_text().startswith("# Managed by DevStack v0.7+"):
            raise MCPManagementError("unit_collision")
        _atomic(target, body)
    run_command("systemctl", "daemon-reload", timeout=30)
    run_command("systemctl", "enable", "devstack-network-reconcile.timer", timeout=30)


def _public_env(host: str, prefs: dict) -> bool:
    value = ("DEVSTACK_MCP_PUBLIC_HOST=" + host + "\n"
             + "DEVSTACK_PUBLIC_MCP=" + str(int(prefs["mcp"])) + "\n"
             + "DEVSTACK_PUBLIC_WEBHOOKS=" + str(int(prefs["webhooks"])) + "\n")
    try:
        if ENV_FILE.read_text(encoding="utf-8") == value:
            return False
    except FileNotFoundError:
        pass
    _atomic(ENV_FILE, value)
    return True


def _ready_public(host: str, prefs: dict):
    """Start only when necessary; do not reset MCP state on every timer tick."""
    changed = _public_env(host, prefs)
    run_command("systemctl", "enable", "devstack-public.service", timeout=20)
    try:
        active = run_command("systemctl", "is-active", "devstack-public.service").strip() == "active"
    except MCPManagementError:
        active = False
    if changed or not active:
        run_command("systemctl", "restart", "devstack-public.service", timeout=40)
    from urllib.request import urlopen
    import time
    for _ in range(30):
        try:
            with urlopen("http://127.0.0.1:8768/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(.2)
    raise MCPManagementError("public_listener_not_ready")


def _finish_bootstrap():
    """Remove first-use port 80 and retire old Tailscale timer once private works."""
    if Path("/etc/devstack/bootstrap.env").exists():
        run_command("python3", "/usr/local/lib/devstack/bootstrap.py", "--finish", timeout=45)
    run_command("systemctl", "disable", "--now",
                "devstack-tailscale-finish.timer", timeout=15)


@contextmanager
def network_lock():
    """Serialize updater, timer, and root-broker route changes."""
    if LOCK_FILE.is_symlink():
        raise MCPManagementError("network_lock_unsafe")
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def reconcile():
    with network_lock():
        return _reconcile_locked()


def _reconcile_locked():
    """Bounded migration, safely repeatable after every interrupted checkpoint."""
    try:
        host = pinned_hostname()
    except MCPManagementError as exc:
        # Fresh cloud-init servers may not be connected to Tailscale yet.
        if exc.code in ("tailscale_disconnected", "private_tailscale_not_configured"):
            return "awaiting_tailscale"
        raise
    phase, old_hooks, old_mcp = state(host)
    prefs = read_preferences()
    if prefs is None:
        prefs = {"mcp": old_mcp, "webhooks": old_hooks}
        write_preferences(prefs)
    if phase == "empty":
        run_command("tailscale", "serve", "--bg", "--https=8443", PRIVATE, timeout=45)
        phase, _, _ = state(host)
    if phase == "legacy" and old_hooks:
        # Preserve intent in PREFS before releasing external 8443 for the dashboard.
        run_command("tailscale", "funnel", "--https=8443", "off", timeout=30)
        phase, _, old_mcp = state(host)
        if phase != "legacy":
            raise MCPManagementError("legacy_webhook_release_unverified")
    if phase == "legacy":
        run_command("tailscale", "serve", "--bg", "--https=8443", PRIVATE, timeout=45)
        phase, _, old_mcp = state(host)
        if phase != "dual_private":
            raise MCPManagementError("private_cutover_unverified")
        log_event("private_8443_ready")
    if phase == "dual_private":
        run_command("tailscale", "serve", "--https=443", "off", timeout=30)
        phase, _, old_mcp = state(host)
        if phase != "private_ready":
            raise MCPManagementError("old_private_443_not_removed")
    if phase in ("private_ready", "new", "public_ready"):
        # The v0.7.2 MCP service owns the same loopback port as the new
        # consolidated public service. Retire it before starting its successor.
        run_command("systemctl", "disable", "--now",
                    "devstack-mcp.service", timeout=35)
        _ready_public(host, prefs)
    if phase == "private_ready" and any(prefs.values()):
        run_command("tailscale", "funnel", "--yes", "--bg",
                    "--https=443", PUBLIC, timeout=45)
        phase, _, old_mcp = state(host)
        if phase not in ("new", "public_ready"):
            raise MCPManagementError("public_443_unverified")
    if phase in ("new", "public_ready") and not any(prefs.values()):
        run_command("tailscale", "funnel", "--https=443", "off", timeout=30)
        phase, _, old_mcp = state(host)
        if phase != "private_ready":
            raise MCPManagementError("public_443_not_removed")
    if old_mcp:
        run_command("tailscale", "funnel", "--https=10000", "off", timeout=30)
        phase, _, old_mcp = state(host)
        if old_mcp:
            raise MCPManagementError("legacy_mcp_not_removed")
    if phase not in ("private_ready", "new"):
        raise MCPManagementError("network_migration_incomplete")
    _finish_bootstrap()
    log_event("network_reconciled")
    return "network_reconciled"


def update_preferences(name: str, enabled: bool):
    if name not in ("mcp", "webhooks") or type(enabled) is not bool:
        raise MCPManagementError("invalid_action")
    with network_lock():
        current = read_preferences()
        if current is None:
            raise MCPManagementError("network_not_installed")
        current[name] = enabled
        write_preferences(current)
        return _reconcile_locked()


def summary():
    try:
        # Status runs under the unprivileged Web identity. Do not read the
        # root-only PIN_FILE here; mutating reconcile() still requires it.
        host = hostname_from_status(json_command("tailscale", "status", "--json"))
        phase, legacy_hooks, legacy_mcp = state(host)
        prefs = read_preferences()
        if prefs is None:
            prefs = {"mcp": legacy_mcp, "webhooks": legacy_hooks}
        public_active = phase == "new" and any(prefs.values())
        return {"phase": "new" if phase in ("new", "private_ready") else phase,
                "public_active": public_active, "mcp": prefs["mcp"],
                "webhooks": prefs["webhooks"], "private_url":
                f"https://{host}:8443/" if phase not in ("legacy", "empty") else
                f"https://{host}/", "url": f"https://{host}/mcp"
                if prefs["mcp"] and public_active else None}
    except MCPManagementError:
        return {"phase": "unknown", "public_active": False, "mcp": False, "webhooks": False,
                "private_url": None, "url": None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    options = parser.parse_args()
    if os.geteuid() != 0 or options.prepare == options.reconcile:
        parser.error("root-only exact action required")
    if options.prepare:
        prepare()
    else:
        print(reconcile())


if __name__ == "__main__":
    main()
