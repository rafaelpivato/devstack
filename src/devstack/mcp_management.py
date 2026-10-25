"""Fixed-purpose MCP gateway/Tailscale Funnel lifecycle, no arbitrary host commands.

The private Web UI can only enqueue 'enable' or 'disable'; a root-owned
systemd.path oneshot interprets those two exact verbs, owns port 10000 only,
and never replaces the tailnet-only dashboard or webhook configuration.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import tempfile
import time

REQUEST_DIR = Path("/var/lib/devstack-web/mcp-request")
REQUEST_FILE = REQUEST_DIR / "action"
RESULT_FILE = Path("/run/devstack-mcp-result.json")
ENV_FILE = Path("/run/devstack-mcp.env")
PORT = 10000
TARGET = "http://127.0.0.1:8768"
PIN_FILE = Path("/var/lib/devstack/tailscale-identity.json")
HOST_PATTERN = re.compile(r"(?:[a-z0-9-]+\.)+ts\.net\Z")

# Literal units, installed only from a verified release wheel by the root
# updater's existing CLI-install hook. Nothing below is supplied by MCP clients.
UNIT_FILES = {
    "devstack-mcp.service": """# Managed by DevStack v0.7+
[Unit]
Description=DevStack MCP gateway (opt-in)
After=devstack-web-v2.service tailscaled.service
Requires=devstack-web-v2.service

[Service]
Type=simple
DynamicUser=yes
EnvironmentFile=/run/devstack-mcp.env
ExecStart=/opt/devstack/current/venv/bin/devstack-mcp
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=strict
PrivateDevices=yes
PrivateTmp=yes
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077

[Install]
WantedBy=multi-user.target
""",
    "devstack-mcp-apply.service": """# Managed by DevStack v0.7+
[Unit]
Description=Apply a bounded private DevStack MCP exposure request
After=devstack-web-v2.service

[Service]
Type=oneshot
User=root
ExecStart=/opt/devstack/current/venv/bin/python -m devstack.mcp_management --apply
NoNewPrivileges=yes
PrivateTmp=yes
TimeoutStartSec=90
""",
    "devstack-mcp-apply.path": """# Managed by DevStack v0.7+
[Unit]
Description=Watch private MCP exposure requests
After=devstack-web-v2.service

[Path]
PathExists=/var/lib/devstack-web/mcp-request/action
Unit=devstack-mcp-apply.service

[Install]
WantedBy=multi-user.target
""",
}


class MCPManagementError(RuntimeError):
    """Safe, bounded diagnostic code for the MCP exposure controls."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def run_command(*args: str, timeout: int = 12) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                check=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MCPManagementError("host_command_failed") from exc
    return result.stdout


def json_command(*args: str) -> dict:
    try:
        data = json.loads(run_command(*args))
    except (ValueError, TypeError) as exc:
        raise MCPManagementError("invalid_tailscale_response") from exc
    if not isinstance(data, dict):
        raise MCPManagementError("invalid_tailscale_response")
    return data


def hostname_from_status(data: dict) -> str:
    if data.get("BackendState") != "Running":
        raise MCPManagementError("tailscale_disconnected")
    host = (data.get("Self") or {}).get("DNSName", "")
    if not isinstance(host, str):
        raise MCPManagementError("tailscale_identity_invalid")
    host = host.rstrip(".").lower()
    if not HOST_PATTERN.fullmatch(host):
        raise MCPManagementError("tailscale_identity_invalid")
    return host


def pinned_hostname() -> str:
    """Avoid silently publishing through a changed Tailscale identity."""
    status = json_command("tailscale", "status", "--json")
    host = hostname_from_status(status)
    try:
        saved = json.loads(PIN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MCPManagementError("private_tailscale_not_configured") from exc
    current = {
        "node_id": (status.get("Self") or {}).get("ID"),
        "tailnet": (status.get("CurrentTailnet") or {}).get("Name"),
    }
    if saved != current:
        raise MCPManagementError("tailscale_identity_changed")
    return host


def serve_state(host: str, state: dict) -> tuple[bool, bool]:
    """Return (webhooks_enabled, mcp_enabled), verifying every served port.

    Only 443 private DevStack, optional 8443 webhooks, and optional 10000 MCP
    are permitted. Never modify unknown or foreign Serve/Funnel routes.
    """
    if set(state) - {"TCP", "Web", "AllowFunnel"}:
        raise MCPManagementError("unknown_tailscale_routes")
    tcp = state.get("TCP") or {}
    web = state.get("Web") or {}
    funnel = state.get("AllowFunnel") or {}
    if not isinstance(tcp, dict) or not isinstance(web, dict) or not isinstance(funnel, dict):
        raise MCPManagementError("unknown_tailscale_routes")
    webhooks = host + ":8443" in web or "8443" in tcp
    mcp = host + ":10000" in web or "10000" in tcp
    expected_tcp = {p: {"HTTPS": True} for p in
                    (["443"] + (["8443"] if webhooks else []) + (["10000"] if mcp else []))}
    expected_web = {
        host + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}},
    }
    if webhooks:
        expected_web[host + ":8443"] = {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}}
    if mcp:
        expected_web[host + ":10000"] = {"Handlers": {"/": {"Proxy": TARGET}}}
    expected_funnel = ({host + ":8443": True} if webhooks else {}) | (
        {host + ":10000": True} if mcp else {})
    if tcp != expected_tcp or web != expected_web or funnel != expected_funnel:
        raise MCPManagementError("unknown_tailscale_routes")
    return webhooks, mcp


def result_record(code: str, *, url: str | None = None) -> None:
    """Root writes only finite status codes and a validated public DNS name."""
    if not re.fullmatch(r"[a-z_]{1,45}", code):
        code = "unknown_error"
    record = {"code": code, "url": url}
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=RESULT_FILE.parent,
        prefix=".devstack-mcp-", delete=False,
    ) as stream:
        name = stream.name
        json.dump(record, stream)
        stream.write("\n")
        stream.flush()
        os.fchmod(stream.fileno(), 0o644)
    os.replace(name, RESULT_FILE)


def verify_state(host: str) -> tuple[bool, bool]:
    return serve_state(host, json_command("tailscale", "serve", "status", "--json"))


def apply_action(action: str) -> str:
    if action not in {"enable", "disable"}:
        raise MCPManagementError("invalid_action")
    host = pinned_hostname()
    _, active = verify_state(host)
    if action == "enable":
        url = f"https://{host}:10000/mcp"
        if not active:
            # The unit owns no privileged sockets; hostname is not user input.
            ENV_FILE.write_text(f"DEVSTACK_MCP_PUBLIC_HOST={host}\n", encoding="ascii")
            ENV_FILE.chmod(0o644)
            try:
                run_command("systemctl", "enable", "--now", "devstack-mcp.service", timeout=40)
                # Ensure the gateway actually accepts loopback TCP first.
                import socket
                for attempt in range(24):
                    try:
                        with socket.create_connection(("127.0.0.1", 8768), timeout=.15):
                            break
                    except OSError:
                        time.sleep(.15)
                else:
                    raise MCPManagementError("gateway_not_ready")
                run_command("tailscale", "funnel", "--yes", "--bg",
                            "--https=10000", TARGET, timeout=45)
                if not verify_state(host)[1]:
                    raise MCPManagementError("funnel_not_verified")
            except MCPManagementError:
                # Never leave the public tunnel configured if verification failed.
                try:
                    if verify_state(host)[1]:
                        run_command("tailscale", "funnel", "--https=10000",
                                    "off", timeout=30)
                except MCPManagementError:
                    pass  # Unknown route: never clobber it, show failure.
                run_command("systemctl", "disable", "--now", "devstack-mcp.service",
                            timeout=30)
                raise
        else:
            # Restart a previously configured gateway after a host reboot.
            if not ENV_FILE.exists():
                ENV_FILE.write_text(f"DEVSTACK_MCP_PUBLIC_HOST={host}\n",
                                    encoding="ascii")
                ENV_FILE.chmod(0o644)
            run_command("systemctl", "enable", "--now", "devstack-mcp.service", timeout=40)
        return url
    if active:
        run_command("tailscale", "funnel", "--https=10000", "off", timeout=30)
        if verify_state(host)[1]:
            raise MCPManagementError("funnel_not_removed")
    run_command("systemctl", "disable", "--now", "devstack-mcp.service", timeout=30)
    return "disabled"


def apply_pending() -> None:
    """Consume and validate one operator action from a private web-owned file."""
    try:
        metadata = REQUEST_FILE.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or
                stat.S_IMODE(metadata.st_mode) != 0o600 or
                metadata.st_uid != pwd.getpwnam("devstack").pw_uid):
            raise MCPManagementError("request_invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(REQUEST_FILE, flags), "rb") as source:
            data = source.read(16)
        REQUEST_FILE.unlink()
        if data not in (b"enable", b"disable"):
            raise MCPManagementError("request_invalid")
        action = data.decode("ascii")
        url = apply_action(action)
        result_record("enabled" if action == "enable" else "disabled",
                      url=url if action == "enable" else None)
    except MCPManagementError as exc:
        result_record(exc.code)
        raise


def request_action(action: str) -> None:
    """Existing web CSRF is the request authority; no user commands or URLs."""
    if action not in ("enable", "disable"):
        raise MCPManagementError("invalid_action")
    directory = REQUEST_DIR
    try:
        metadata = directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or
                stat.S_IMODE(metadata.st_mode) != 0o700 or
                metadata.st_uid != os.getuid()):
            raise MCPManagementError("request_directory_unsafe")
    except FileNotFoundError as exc:
        raise MCPManagementError("mcp_setup_missing") from exc
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(directory / "action", flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(action.encode("ascii"))
    except FileExistsError as exc:
        raise MCPManagementError("request_pending") from exc
    except OSError as exc:
        raise MCPManagementError("request_failed") from exc


def status() -> dict:
    """Public-safe management summary; no shell, token, or full Serve dump."""
    result = {}
    try:
        result = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            result = {}
    except (OSError, ValueError):
        pass
    last_code = result.get("code")
    if not isinstance(last_code, str) or not re.fullmatch("[a-z_]{1,45}", last_code):
        last_code = None
    connected_url = None
    tunnel = "unknown"
    try:
        state = json_command("tailscale", "status", "--json")
        host = hostname_from_status(state)
        _, active = verify_state(host)
        tunnel = "enabled" if active else "disabled"
        connected_url = f"https://{host}:10000/mcp" if active else None
    except MCPManagementError:
        pass
    try:
        active = run_command("systemctl", "is-active", "devstack-mcp.service")
        gateway = "running" if active.strip() == "active" else "stopped"
    except MCPManagementError:
        gateway = "stopped"
    return {
        "gateway": gateway,
        "funnel": tunnel,
        "url": connected_url,
        "request_pending": REQUEST_FILE.exists(),
        "last_result": last_code,
        "installed": REQUEST_DIR.is_dir(),
    }


def install_units(root: Path = Path("/opt/devstack"),
                  unit_dir: Path = Path("/etc/systemd/system"),
                  request_dir: Path = REQUEST_DIR,
                  user: str = "devstack",
                  run_systemd: bool = True) -> None:
    """Called by the *existing root updater CLI hook* for each published wheel.

    Refuse collisions with non-DevStack systemd units. Never grant web direct
    Tailscale operator permission. Do not start the gateway or Funnel here.
    """
    root = Path(root)
    if root != Path("/opt/devstack") and run_systemd:
        raise MCPManagementError("nonproduction_install")
    if os.geteuid() != 0 and run_systemd:
        raise MCPManagementError("root_required")
    account = pwd.getpwnam(user)
    request_dir = Path(request_dir)
    if request_dir.is_symlink():
        raise MCPManagementError("request_directory_unsafe")
    if request_dir.exists():
        data = request_dir.stat()
        if (not stat.S_ISDIR(data.st_mode) or data.st_uid != account.pw_uid
                or stat.S_IMODE(data.st_mode) != 0o700):
            raise MCPManagementError("request_directory_unsafe")
    else:
        request_dir.mkdir(parents=True, mode=0o700)
        os.chown(request_dir, account.pw_uid, account.pw_gid)
        request_dir.chmod(0o700)
    for name, body in UNIT_FILES.items():
        destination = Path(unit_dir) / name
        if destination.is_symlink():
            raise MCPManagementError("unit_collision")
        if destination.exists():
            if not destination.is_file() or not destination.read_text().startswith(
                    "# Managed by DevStack v0.7+"):
                raise MCPManagementError("unit_collision")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=unit_dir,
            prefix=".devstack-mcp-unit-", delete=False,
        ) as stream:
            tmp = stream.name
            stream.write(body)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
        os.replace(tmp, destination)
    if run_systemd:
        run_command("systemctl", "daemon-reload", timeout=25)
        run_command("systemctl", "enable", "--now", "devstack-mcp-apply.path",
                    timeout=25)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="DevStack MCP restricted host broker")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply or os.geteuid() != 0:
        parser.error("Only the root-owned fixed-purpose systemd service may apply")
    apply_pending()


if __name__ == "__main__":
    main()
