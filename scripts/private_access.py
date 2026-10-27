#!/usr/bin/env python3
"""Conservatively configure tailnet-only HTTPS for the Devstack dashboard.

No cloud-provider API, application release, or SSH server is required.
Only the Tailscale CLI needs to be installed and authenticated separately.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
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


def is_our_private_serve(status: dict[str, Any], port: int = 8443) -> bool:
    if set(status) - {"TCP", "Web", "AllowFunnel"}:
        return False
    if status.get("TCP") != {str(port): {"HTTPS": True}}:
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


def pin_initial_identity(status: dict[str, Any]) -> None:
    """Trust-on-first-use pin; reject changed tailnets/devices on later runs.

    This detects *later* re-enrollment. It cannot establish who controlled
    a brand-new VM before the first approved authentication.
    """
    node = status.get("Self") or {}
    tailnet = status.get("CurrentTailnet") or {}
    if not isinstance(node, dict) or not isinstance(tailnet, dict):
        raise PrivateAccessError("Cannot read Tailscale identity")
    identity = {"node_id": node.get("ID"), "tailnet": tailnet.get("Name")}
    if not all(isinstance(v, str) and v for v in identity.values()):
        raise PrivateAccessError("Tailscale identity is incomplete; refusing enrollment")
    path = Path(os.environ.get(
        "DEVSTACK_TAILSCALE_IDENTITY", "/var/lib/devstack/tailscale-identity.json"
    ))
    if path.is_symlink():
        raise PrivateAccessError("Refusing identity file symlink")
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PrivateAccessError("Existing identity pin is invalid") from exc
        if saved != identity:
            raise PrivateAccessError(
                "Tailscale device or tailnet changed after initial enrollment; "
                "refusing private proxy configuration. Investigate via recovery console."
            )
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(identity, output)
            output.write("\n")


def configure() -> str:
    status = parse_json(run_cli("status", "--json"))
    hostname = connected_hostname(status)
    pin_initial_identity(status)
    serve_state = parse_json(run_cli("serve", "status", "--json"))
    if is_our_private_serve(serve_state, 8443):
        return f"Private dashboard already configured: https://{hostname}:8443/"
    if is_our_private_serve(serve_state, 443):
        return f"Legacy private dashboard retained at https://{hostname}/; migrate from the DevStack Web UI"
    if not is_empty_serve(serve_state):
        raise PrivateAccessError(
            "Existing Tailscale Serve/Funnel configuration detected; "
            "refusing to replace or expose it. Review 'tailscale serve status'."
        )
    # Serve, not Funnel: reachable only within the permitted tailnet.
    run_cli("serve", "--bg", "--https=8443", LOCAL_TARGET)
    return f"Private dashboard: https://{hostname}:8443/"


def finish_if_connected() -> str:
    """Used by a systemd timer after first browser approval; never re-enroll."""
    state = parse_json(run_cli("status", "--json"))
    if state.get("BackendState") != "Running":
        return "Awaiting first Tailscale browser authorization"
    result = configure()
    # Close temporary port 80 only after a successfully configured private
    # HTTPS route. The service is stopped before its secret is deleted.
    if Path("/etc/devstack/bootstrap.env").exists():
        try:
            subprocess.run(
                ["/usr/bin/python3", "/usr/local/lib/devstack/bootstrap.py", "--finish"],
                capture_output=True, text=True, check=True, timeout=45,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise PrivateAccessError("Private HTTPS is active but bootstrap cleanup failed") from exc
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-connected", action="store_true",
        help="Exit 0 when authenticated to a tailnet, 1 otherwise"
    )
    parser.add_argument(
        "--finish-if-connected", action="store_true",
        help="Complete private Serve after browser activation, without re-authenticating"
    )
    args = parser.parse_args()
    try:
        if args.finish_if_connected:
            print(finish_if_connected())
        elif args.check_connected:
            connected_hostname(parse_json(run_cli("status", "--json")))
        else:
            print(configure())
        return 0
    except PrivateAccessError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
