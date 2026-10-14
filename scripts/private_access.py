#!/usr/bin/env python3
"""Conservatively configure tailnet-only HTTPS for the Devstack dashboard.

No cloud-provider API, application release, or SSH server is required.
Only the Tailscale CLI needs to be installed and authenticated separately.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

LOCAL_TARGET = "http://127.0.0.1:8765"
DNS_RE = re.compile(r"(?:[a-z0-9-]+\.)+ts\.net\.?\Z", re.I)


class PrivateAccessError(RuntimeError):
    pass


def run_cli(*args: str) -> str:
    try:
        result = subprocess.run(
            ["tailscale", *args], capture_output=True, text=True, check=True, timeout=40
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PrivateAccessError(f"Tailscale command failed: {exc}") from exc
    return result.stdout


def parse_json(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PrivateAccessError("Invalid JSON from Tailscale") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise PrivateAccessError("Unexpected Tailscale response")
    return parsed


def connected_hostname(status: dict[str, Any]) -> str:
    if status.get("BackendState") != "Running":
        raise PrivateAccessError("Tailscale is not connected; run 'sudo tailscale up'")
    raw = status.get("Self") or {}
    if not isinstance(raw, dict):
        raise PrivateAccessError("Tailscale identity is missing")
    hostname = str(raw.get("DNSName", "")).rstrip(".").lower()
    if not DNS_RE.fullmatch(hostname):
        raise PrivateAccessError("Tailscale DNS name is invalid or unavailable")
    return hostname


def is_empty_serve(status: dict[str, Any]) -> bool:
    if not status:
        return True
    # Empty JSON objects in older versions may include zero-valued keys.
    return (set(status).issubset({"TCP", "Web", "AllowFunnel"})
            and not any(status.values()))


def is_our_private_serve(status: dict[str, Any]) -> bool:
    if set(status) - {"TCP", "Web", "AllowFunnel"}:
        return False
    if status.get("TCP") != {"443": {"HTTPS": True}}:
        return False
    if any((status.get("AllowFunnel") or {}).values()):
        return False  # Do not treat a public Funnel as authenticated/private.
    web = status.get("Web") or {}
    if not isinstance(web, dict) or len(web) != 1:
        return False
    entry = next(iter(web.values()))
    return isinstance(entry, dict) and entry.get("Handlers") == {
        "/": {"Proxy": LOCAL_TARGET}
    }


def configure() -> str:
    hostname = connected_hostname(parse_json(run_cli("status", "--json")))
    serve_state = parse_json(run_cli("serve", "status", "--json"))
    if is_our_private_serve(serve_state):
        return f"Private dashboard already configured: https://{hostname}/"
    if not is_empty_serve(serve_state):
        raise PrivateAccessError(
            "Existing Tailscale Serve/Funnel configuration detected; "
            "refusing to replace or expose it. Review 'tailscale serve status'."
        )
    # Serve, not Funnel: reachable only within the permitted tailnet.
    run_cli("serve", "--bg", "--https=443", LOCAL_TARGET)
    return f"Private dashboard: https://{hostname}/"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-connected", action="store_true",
        help="Exit 0 when authenticated to a tailnet, 1 otherwise"
    )
    args = parser.parse_args()
    try:
        if args.check_connected:
            connected_hostname(parse_json(run_cli("status", "--json")))
        else:
            print(configure())
        return 0
    except PrivateAccessError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
