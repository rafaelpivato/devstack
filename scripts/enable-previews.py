#!/usr/bin/env python3
"""Opt in to a separate tailnet-only HTTPS origin for registered previews.

Requires the reviewed installer refresh and an existing exact private Serve
dashboard mapping. Never configures Funnel or changes port 443.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

import private_access as pa


def enable() -> str:
    status = pa.parse_json(pa.run_cli("status", "--json"))
    hostname = pa.connected_hostname(status)
    pa.pin_initial_identity(status)
    current = pa.parse_json(pa.run_cli("serve", "status", "--json"))
    if pa.preview_serve_active(current):
        return f"Previews already available: https://{hostname}:8443/"
    if not pa.is_our_private_serve(current):
        raise pa.PrivateAccessError(
            "Tailscale Serve configuration is not the expected private Devstack "
            "dashboard mapping. Refusing changes."
        )
    try:
        with urllib.request.urlopen("http://127.0.0.1:8766/healthz",
                                    timeout=4) as response:
            data = json.load(response)
            if data.get("scope") != "previews-only":
                raise pa.PrivateAccessError("Unexpected preview listener response")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise pa.PrivateAccessError(
            "Preview listener is unavailable. Re-run the reviewed installer "
            "and check devstack-preview.service first."
        ) from exc
    pa.run_cli("serve", "--bg", "--https=8443", pa.PREVIEW_TARGET)
    updated = pa.parse_json(pa.run_cli("serve", "status", "--json"))
    if not pa.preview_serve_active(updated):
        raise pa.PrivateAccessError(
            "Preview mapping could not be confirmed; inspect Tailscale Serve "
            "and remove the 8443 rule manually if needed."
        )
    return f"Private previews enabled: https://{hostname}:8443/"


def main() -> int:
    try:
        print(enable())
        return 0
    except pa.PrivateAccessError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
