"""Bounded MCP/Funnel operations and explicit guarded Tailscale port migration.

The private UI queues only fixed verbs. The root broker validates all known
routes, preserves private dashboard access and never accepts arbitrary commands.
"""
from __future__ import annotations

import errno
import json
import sys
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import tempfile
import time

REQUEST_DIR = Path("/run/devstack-update-request")
REQUEST_FILE = REQUEST_DIR / "mcp-action"
RESULT_FILE = Path("/run/devstack-mcp-result.json")
ENV_FILE = Path("/etc/devstack-mcp.env")
SNAPSHOT_FILE = Path("/run/devstack-mcp-diagnostics.json")
PORT = 443
TARGET = "http://127.0.0.1:8768"
from devstack.tailscale_layout import identify, MCP_PUBLIC, WEBHOOK_PUBLIC, DASHBOARD
PIN_FILE = Path("/var/lib/devstack/tailscale-identity.json")
HOST_PATTERN = re.compile(r"(?:[a-z0-9-]+\.)+ts\.net\Z")

# Literal units, installed only from a verified release wheel by the root
# updater's existing CLI-install hook. Nothing below is supplied by MCP clients.
UNIT_FILES = {
    "devstack-mcp.service": """# Managed by DevStack v0.7+
[Unit]
Description=DevStack MCP gateway (opt-in)
After=devstack-control.service tailscaled.service
Requires=devstack-control.service

[Service]
Type=simple
DynamicUser=yes
SupplementaryGroups=devstackipc
EnvironmentFile=/etc/devstack-mcp.env
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
TimeoutStartSec=240
""",
    "devstack-mcp-diagnostics.service": """# Managed by DevStack v0.7+
[Unit]
Description=Collect bounded MCP-specific diagnostic events
After=systemd-journald.service

[Service]
Type=oneshot
User=root
ExecStart=/opt/devstack/current/venv/bin/python -m devstack.mcp_management --capture
NoNewPrivileges=yes
PrivateTmp=yes
TimeoutStartSec=20
""",
    "devstack-mcp-diagnostics.timer": """# Managed by DevStack v0.7+
[Unit]
Description=Refresh MCP support diagnostics for the private Web console

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=10s
Unit=devstack-mcp-diagnostics.service

[Install]
WantedBy=timers.target
""",
    "devstack-mcp-apply.path": """# Managed by DevStack v0.7+
[Unit]
Description=Watch private MCP exposure requests
After=devstack-web-v2.service

[Path]
PathExists=/run/devstack-update-request/mcp-action
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


def log_event(code: str) -> None:
    """Only allowlisted fixed-shape codes are emitted; no request payloads."""
    if not re.fullmatch(r"[a-z_]{3,55}", code):
        code = "invalid_event"
    print("DEVSTACK_MCP " + code, file=sys.stderr, flush=True)


def capture_diagnostics() -> None:
    """Root snapshots exact MCP units only, never arbitrary host-journal text.

    Store at most 120 structured events. Unrecognized/variable messages are
    never copied into the Web-readable file; generic unit errors are signaled.
    """
    try:
        output = run_command(
            "journalctl", "--no-pager", "--output=json", "--since=-30min",
            "--lines=240", "--unit=devstack-mcp.service",
            "--unit=devstack-mcp-apply.service",
            "--unit=devstack-public.service", timeout=8,
        )
        available = True
    except MCPManagementError:
        output, available = "", False
    now = time.time_ns() // 1_000_000
    events = []
    for line in output.splitlines()[-240:]:
        if len(line) > 8192:
            continue
        try:
            entry = json.loads(line)
            microseconds = int(entry.get("__REALTIME_TIMESTAMP", 0))
        except (ValueError, TypeError):
            continue
        when = microseconds // 1000
        if not (now - 30*60*1000 <= when <= now + 30000):
            continue
        level = "error" if str(entry.get("PRIORITY")) in ("0", "1", "2", "3") else "info"
        unit = str(entry.get("_SYSTEMD_UNIT") or entry.get("UNIT") or "")
        if unit not in {"devstack-mcp.service", "devstack-mcp-apply.service", "devstack-public.service"}:
            continue
        message = entry.get("MESSAGE", "")
        if not isinstance(message, str):
            continue
        match = re.fullmatch(r"DEVSTACK_MCP ([a-z_]{3,55})", message)
        if match:
            code = match.group(1)
            if code.startswith(("funnel_error_", "gateway_process_error", "status_unavailable")):
                level = "error"
        elif level == "error":
            code = ("gateway_process_error" if unit == "devstack-mcp.service"
                    else "public_gateway_error" if unit == "devstack-public.service"
                    else "funnel_broker_error")
        else:
            continue
        # Each line has only an allowlisted structure and a fixed unit identity.
        events.append({"at": when, "source": "mcp",
                       "level": level, "code": "mcp-" + code.replace("_", "-")})
    events.sort(key=lambda e: e["at"])
    truncated = len(events) > 120
    from devstack.diagnostics_bridge import capture_auxiliary
    doc = {"generated_at": now, "available": available,
           "events": events[-120:], "truncated": truncated,
           "auxiliary": capture_auxiliary()}
    atomic_result_file(SNAPSHOT_FILE, doc)


def atomic_result_file(path: Path, payload: dict) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=".devstack-mcp-", delete=False,
    ) as stream:
        name = stream.name
        json.dump(payload, stream, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fchmod(stream.fileno(), 0o644)
    os.replace(name, path)


def classified_command_error(args: tuple[str, ...], exc: BaseException) -> str:
    """Categorize known host errors; never expose raw stderr or command output.

    Funnel may wait on first-use tailnet approval or HTTPS certificate setup.
    Timeouts and policy failures must be independently actionable in the Web
    console; generic failures must not be mistaken for missing permissions.
    """
    tailscale = args[:1] == ("tailscale",)
    funnel = args[:2] == ("tailscale", "funnel")
    if isinstance(exc, subprocess.TimeoutExpired):
        if funnel:
            return "funnel_setup_timeout"
        return "tailscale_command_timeout" if tailscale else "host_command_timeout"

    # Some clients write bytes, others text. Only classify fixed, safe phrases.
    stderr = getattr(exc, "stderr", "") or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    detail = str(stderr).lower()
    if tailscale:
        if any(s in detail for s in ("not running", "not connected", "logged out")):
            return "tailscale_disconnected"
        if funnel:
            # HTTPS and certificate errors may also say "not enabled".
            # Classify these specific failures before broad permission text.
            if any(s in detail for s in (
                "https is not enabled", "https must be enabled", "magicdns",
            )):
                return "funnel_https_required"
            if any(s in detail for s in (
                "certificate", "acme", "let's encrypt", "lets encrypt", "rate limit",
            )):
                return "funnel_certificate_failed"
            if any(s in detail for s in (
                "not allowed", "permission", "not authorized", "not permitted",
                "funnel is disabled", "funnel is not enabled", "enable funnel",
                "requires approval", "not enabled for",
            )):
                return "funnel_permission_required"
            if any(s in detail for s in (
                "port already in use", "listener already exists", "address already in use",
            )):
                return "funnel_port_in_use"
            if any(s in detail for s in ("deadline exceeded", "timed out", "timeout")):
                return "funnel_setup_timeout"
            return "funnel_command_failed"
        return "tailscale_command_failed"
    if args[:1] == ("systemctl",):
        return "systemd_command_failed"
    if args[:1] == ("journalctl",):
        return "mcp_journal_unavailable"
    return "host_command_failed"


def run_command(*args: str, timeout: int = 12) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                check=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MCPManagementError(classified_command_error(args, exc)) from exc
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


def topology(host: str):
    try:
        return identify(host, json_command("tailscale", "serve", "status", "--json"))
    except ValueError as exc:
        raise MCPManagementError("unknown_tailscale_routes") from exc


def serve_state(host: str, state: dict) -> tuple[bool, bool]:
    """Exact allowlist; transitional states require the explicit migrate action."""
    try:
        entry = identify(host, state)
    except ValueError as exc:
        raise MCPManagementError("unknown_tailscale_routes") from exc
    if entry.phase not in ("legacy", "new"):
        raise MCPManagementError("migration_incomplete")
    return entry.webhooks, entry.mcp


def migrate_ports(host: str) -> str:
    """Resumable cutover. Private :8443 is validated *before* freeing :443.

    Active old :8443 webhooks block migration: moving them could break signed
    third-party callbacks. No public command runs while private :443 exists.
    """
    entry = topology(host)
    if entry.phase == "new":
        return f"https://{host}:8443/"
    if entry.webhooks:
        raise MCPManagementError("legacy_webhooks_must_be_disabled")
    if entry.phase == "legacy":
        run_command("tailscale", "serve", "--bg", "--https=8443", DASHBOARD, timeout=45)
        if topology(host).phase != "dual_private":
            raise MCPManagementError("private_cutover_unverified")
        log_event("private_8443_ready")
        entry = topology(host)
    if entry.phase == "dual_private":
        # Important: explicitly turn off the known private handler BEFORE
        # Funnel is allowed to allocate public HTTPS :443.
        run_command("tailscale", "serve", "--https=443", "off", timeout=30)
        entry = topology(host)
        if entry.phase not in ("private_ready", "new"):
            raise MCPManagementError("old_private_route_not_removed")
        log_event("old_private_443_removed")
    if entry.phase == "private_ready":
        # Old MCP Funnel remains available at :10000 through this phase.
        run_command("tailscale", "funnel", "--yes", "--bg", "--https=443",
                    "--set-path=/mcp", MCP_PUBLIC, timeout=45)
        if topology(host).phase != "public_ready":
            raise MCPManagementError("public_mcp_route_unverified")
        entry = topology(host)
    if entry.phase == "public_ready":
        run_command("tailscale", "funnel", "--https=10000", "off", timeout=30)
        entry = topology(host)
    if entry.phase != "new":
        raise MCPManagementError("migration_incomplete")
    log_event("network_migration_succeeded")
    return f"https://{host}:8443/"


def result_record(code: str, *, url: str | None = None) -> None:
    """Root writes only finite status codes and a validated public DNS name."""
    if not re.fullmatch(r"[a-z_]{1,45}", code):
        code = "unknown_error"
    record = {"code": code, "url": url}
    atomic_result_file(RESULT_FILE, record)


def verify_state(host: str) -> tuple[bool, bool]:
    return serve_state(host, json_command("tailscale", "serve", "status", "--json"))


def apply_action(action: str) -> str:
    """Fixed root broker updates endpoint intent; one network reconciler owns routing."""
    from devstack.networking import update_preferences, summary
    actions = {
        "enable": ("mcp", True), "disable": ("mcp", False),
        "enable_webhooks": ("webhooks", True),
        "disable_webhooks": ("webhooks", False),
    }
    if action not in actions:
        raise MCPManagementError("invalid_action")
    name, enabled = actions[action]
    update_preferences(name, enabled)
    if name == "mcp" and enabled:
        return summary()["url"] or "enabled"
    if name == "webhooks" and enabled:
        return "webhooks_enabled"
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
            data = source.read(17)  # Reject extra bytes after the longest 16-byte verb
        REQUEST_FILE.unlink()
        if data not in (b"enable", b"disable", b"disable_webhooks", b"enable_webhooks"):
            raise MCPManagementError("request_invalid")
        action = data.decode("ascii")
        log_event("funnel_" + action + "_requested")
        url = apply_action(action)
        result_record({"enable": "enabled", "disable": "disabled", "disable_webhooks": "webhooks_disabled", "enable_webhooks": "webhooks_enabled"}[action],
                      url=url if action == "enable" else None)
        log_event("funnel_" + action + "_succeeded")
    except MCPManagementError as exc:
        result_record(exc.code)
        log_event("funnel_error_" + exc.code)
        raise


def check_request_directory(directory: Path = REQUEST_DIR) -> Path:
    """Existing web RuntimeDirectory is already exercised in Ubuntu CI."""
    try:
        metadata = directory.lstat()
    except FileNotFoundError as exc:
        raise MCPManagementError("request_directory_missing") from exc
    except OSError as exc:
        raise MCPManagementError("request_directory_unavailable") from exc
    if (not stat.S_ISDIR(metadata.st_mode) or
            stat.S_IMODE(metadata.st_mode) != 0o700 or
            metadata.st_uid != os.getuid()):
        raise MCPManagementError("request_directory_unsafe")
    if not os.access(directory, os.W_OK | os.X_OK):
        raise MCPManagementError("request_permission_denied")
    return directory


def request_action(action: str) -> None:
    """Existing web CSRF is the request authority; no user commands or URLs."""
    if action not in ("enable", "disable", "disable_webhooks", "enable_webhooks"):
        raise MCPManagementError("invalid_action")
    directory = REQUEST_DIR
    check_request_directory(directory)
    temp = None
    try:
        # PathExists must not wake the root watcher until all bytes are
        # durable and closed. A same-directory hard link is atomic and refuses
        # to overwrite an existing action from another tab or Web worker.
        descriptor, name = tempfile.mkstemp(prefix=".mcp-pending-", dir=directory)
        temp = Path(name)
        with os.fdopen(descriptor, "wb") as output:
            output.write(action.encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
        os.link(temp, directory / "mcp-action")
    except FileExistsError as exc:
        raise MCPManagementError("request_pending") from exc
    except OSError as exc:
        code = {
            errno.EACCES: "request_permission_denied",
            errno.EPERM: "request_permission_denied",
            errno.EROFS: "request_read_only",
            errno.ENOSPC: "request_disk_full",
            errno.EDQUOT: "request_disk_full",
            errno.ENOENT: "request_directory_missing",
        }.get(exc.errno, "request_failed")
        raise MCPManagementError(code) from exc
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def request_state() -> str:
    try:
        check_request_directory()
        return "ready"
    except MCPManagementError as exc:
        return exc.code


def status() -> dict:
    """Private UI summary; routing from the centralized desired-state owner."""
    from devstack.networking import summary
    state = summary()
    result = {}
    try:
        result = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    code = result.get("code") if isinstance(result, dict) else None
    if not isinstance(code, str) or not re.fullmatch("[a-z_]{1,45}", code):
        code = None
    try:
        active = run_command("systemctl", "is-active", "devstack-public.service")
        gateway = "running" if active.strip() == "active" else "stopped"
    except MCPManagementError:
        gateway = "stopped"
    return {
        "gateway": gateway,
        "funnel": "enabled" if state["mcp"] and state["public_active"]
                  else "unknown" if state["phase"] == "unknown" else "disabled",
        "url": state["url"],
        "private_url": state["private_url"],
        "network_phase": state["phase"],
        "migration_required": False,
        "legacy_webhooks_active": False,
        "webhooks_enabled": state["webhooks"],
        "request_pending": REQUEST_FILE.exists(),
        "request_state": request_state(),
        "last_result": code,
        "installed": Path("/etc/systemd/system/devstack-mcp-apply.path").is_file(),
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
        # The Web service owns this existing RuntimeDirectory. Never synthesize
        # it on a deployed host: the service must own its writable sandbox.
        if run_systemd:
            raise MCPManagementError("request_directory_missing")
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
        run_command("systemctl", "enable", "--now", "devstack-mcp-diagnostics.timer",
                    timeout=25)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="DevStack MCP restricted host broker")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or args.apply == args.capture:
        parser.error("Only root-owned fixed-purpose systemd services may apply or capture")
    if args.capture:
        capture_diagnostics()
    else:
        apply_pending()


if __name__ == "__main__":
    main()
