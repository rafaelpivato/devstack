"""Issue #104: bounded, Web-first Tailscale machine naming.

The unprivileged dashboard writes an atomic request. Only the root-owned
systemd watcher can execute the fixed Tailscale hostname operation.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import pwd
import re
import stat
import tempfile
import time

from devstack.mcp_management import (
    MCPManagementError, HOST_PATTERN, atomic_result_file,
    check_request_directory, hostname_from_status, json_command,
    log_event, pinned_hostname, run_command, REQUEST_DIR,
)

REQUEST_FILE = REQUEST_DIR / "tailscale-rename"
RESULT_FILE = Path("/run/devstack-tailscale-rename-result.json")
NAME_RE = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
RESERVED = frozenset({"localhost"})
RESULT_CODES = frozenset({"renamed", "renamed_with_suffix", "no_change", "invalid_name",
    "request_invalid", "stale_hostname", "identity_changed", "routes_unrecognized",
    "routes_changed", "network_unavailable", "tailscale_disconnected",
    "tailscale_command_timeout", "tailscale_command_failed",
    "public_gateway_unavailable", "failed"})


def validate_name(value: object) -> str:
    """A single lower-case DNS label; no shell syntax, FQDNs or reserved names."""
    if not isinstance(value, str) or not NAME_RE.fullmatch(value) or value in RESERVED:
        raise MCPManagementError("invalid_name")
    return value


def valid_dns(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.rstrip(".").lower()
    return value if len(value) <= 253 and HOST_PATTERN.fullmatch(value) else None


def endpoint_urls(host: str) -> dict:
    return {"private_url": f"https://{host}:8443/",
            "mcp_url": f"https://{host}/mcp"}


def _safe_result() -> dict | None:
    try:
        if RESULT_FILE.is_symlink() or RESULT_FILE.stat().st_size > 1024:
            return None
        raw = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("code") not in RESULT_CODES:
        return None
    old, new = valid_dns(raw.get("old_dns")), valid_dns(raw.get("new_dns"))
    if not isinstance(raw.get("at"), int) or raw["at"] < 0:
        return None
    result = {"code": raw["code"], "at": raw["at"],
              "old_dns": old, "new_dns": new}
    if old:
        result["old_urls"] = endpoint_urls(old)
    if new:
        result["new_urls"] = endpoint_urls(new)
    return result


def status() -> dict:
    """Never infer success from the requested name; use live Tailscale DNS."""
    host = None
    try:
        host = hostname_from_status(json_command("tailscale", "status", "--json"))
    except MCPManagementError:
        pass
    from devstack.networking import summary
    network = summary() if host else {}
    return {
        "connected": host is not None,
        "name": host.split(".")[0] if host else None,
        "fqdn": host,
        "private_url": network.get("private_url"),
        "mcp_url": network.get("url"),
        "network_phase": network.get("phase", "unknown"),
        "request_pending": REQUEST_FILE.exists(),
        "last_result": _safe_result(),
    }


def request_rename(name: object, expected_dns: object) -> None:
    name = validate_name(name)
    old = valid_dns(expected_dns)
    if not old or old != expected_dns:
        raise MCPManagementError("stale_hostname")
    # Reject a stale browser tab before queuing; root repeats under lock.
    current = hostname_from_status(json_command("tailscale", "status", "--json"))
    if old != current:
        raise MCPManagementError("stale_hostname")
    check_request_directory()
    encoded = json.dumps({"name": name, "expected_dns": old},
                         separators=(",", ":")).encode("ascii")
    if len(encoded) > 350:
        raise MCPManagementError("request_invalid")
    temp = None
    try:
        descriptor, filename = tempfile.mkstemp(prefix=".tailscale-name-", dir=REQUEST_DIR)
        temp = Path(filename)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.link(temp, REQUEST_FILE)  # no overwrite of another queued operation
    except FileExistsError as exc:
        raise MCPManagementError("request_pending") from exc
    except OSError as exc:
        code = "request_permission_denied" if exc.errno in (errno.EACCES, errno.EPERM) else "request_failed"
        raise MCPManagementError(code) from exc
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def _write_result(code: str, old: str | None, new: str | None):
    if code not in RESULT_CODES:
        code = "failed"
    atomic_result_file(RESULT_FILE, {"at": int(time.time()), "code": code,
                                     "old_dns": valid_dns(old), "new_dns": valid_dns(new)})


def _apply(name: str, expected: str) -> tuple[str, str]:
    from devstack import networking
    with networking.network_lock():
        old = pinned_hostname()  # validates pinned node ID + tailnet
        if old != expected:
            raise MCPManagementError("stale_hostname")
        before = json_command("tailscale", "serve", "status", "--json")
        try:
            phase, _, _ = networking.classify(old, before)
        except MCPManagementError as exc:
            raise MCPManagementError("routes_unrecognized") from exc
        if phase not in ("new", "private_ready"):
            raise MCPManagementError("routes_unrecognized")
        preferences = networking.read_preferences()
        if preferences is None:
            raise MCPManagementError("network_unavailable")
        if name == old.split(".")[0]:
            return "no_change", old
        run_command("tailscale", "set", "--hostname=" + name, timeout=25)
        new = pinned_hostname()  # same device/tailnet, read effective FQDN
        try:
            after_phase, _, _ = networking.classify(
                new, json_command("tailscale", "serve", "status", "--json"))
        except MCPManagementError as exc:
            raise MCPManagementError("routes_unrecognized") from exc
        if after_phase != phase:
            raise MCPManagementError("routes_changed")
        # Existing TLS routing must remain managed. Only refresh the OAuth
        # public gateway's hostname configuration if public ingress is active.
        if phase == "new" and networking._public_env(new, preferences):
            run_command("systemctl", "restart", "devstack-public.service", timeout=40)
        return ("renamed" if new.split(".")[0] == name
                else "renamed_with_suffix"), new


def apply_pending() -> None:
    """Root-only: independently validate ownership, payload and pinned identity."""
    if os.geteuid() != 0:
        raise MCPManagementError("root_required")
    old = new = None
    try:
        meta = REQUEST_FILE.lstat()
        if (not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o600
                or meta.st_uid != pwd.getpwnam("devstack").pw_uid
                or meta.st_size > 350):
            raise MCPManagementError("request_invalid")
        descriptor = os.open(REQUEST_FILE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as source:
            payload = source.read(351)
        REQUEST_FILE.unlink()
        data = json.loads(payload)
        if not isinstance(data, dict) or set(data) != {"name", "expected_dns"}:
            raise MCPManagementError("request_invalid")
        name = validate_name(data["name"])
        old = valid_dns(data["expected_dns"])
        if not old or data["expected_dns"] != old:
            raise MCPManagementError("request_invalid")
        log_event("tailscale_rename_requested")
        code, new = _apply(name, old)
        _write_result(code, old, new)
        log_event("tailscale_rename_succeeded")
    except (OSError, ValueError, TypeError, MCPManagementError) as exc:
        code = exc.code if isinstance(exc, MCPManagementError) else "request_invalid"
        try:
            new = pinned_hostname()
        except MCPManagementError:
            pass
        _write_result(code, old, new)
        log_event("tailscale_rename_failed")
        raise MCPManagementError(code) from exc


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply or os.geteuid() != 0:
        parser.error("root-only fixed rename broker")
    apply_pending()


if __name__ == "__main__":
    main()
