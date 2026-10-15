#!/usr/bin/env python3
"""Explicit public webhook opt-in on Tailscale Funnel 8443."""
from __future__ import annotations
import argparse
import json
import sys
from urllib import request
import private_access as pa

TARGET = "http://127.0.0.1:8767"


def matches_funnel(config, hostname):
    if set(config) - {"TCP", "Web", "AllowFunnel"}:
        return False
    if config.get("TCP") != {"443": {"HTTPS": True}, "8443": {"HTTPS": True}}:
        return False
    expected_web = {
        hostname + ":443": {"Handlers": {"/": {"Proxy": pa.LOCAL_TARGET}}},
        hostname + ":8443": {"Handlers": {"/": {"Proxy": TARGET}}},
    }
    allowed = config.get("AllowFunnel") or {}
    return (config.get("Web") == expected_web
            and isinstance(allowed, dict)
            and allowed.get(hostname + ":8443") is True
            and not any(v for k, v in allowed.items() if k != hostname + ":8443"))


def configure(disable=False):
    status = pa.parse_json(pa.run_cli("status", "--json"))
    hostname = pa.connected_hostname(status)
    pa.pin_initial_identity(status)
    current = pa.parse_json(pa.run_cli("serve", "status", "--json"))
    if disable:
        if pa.is_our_private_serve(current):
            return "Webhook Funnel already disabled"
        if not matches_funnel(current, hostname):
            raise pa.PrivateAccessError("Unknown Serve/Funnel state; no changes made")
        pa.run_cli("funnel", "--https=8443", "off")
        result = pa.parse_json(pa.run_cli("serve", "status", "--json"))
        if not pa.is_our_private_serve(result):
            raise pa.PrivateAccessError("Unable to confirm Funnel was disabled")
        return "Webhook Funnel disabled; private dashboard preserved"
    if matches_funnel(current, hostname):
        return f"Webhooks public at https://{hostname}:8443/hooks/<name>"
    if not pa.is_our_private_serve(current):
        raise pa.PrivateAccessError("Expected a private Devstack dashboard on 443 only")
    try:
        with request.urlopen("http://127.0.0.1:8767/healthz", timeout=5) as response:
            if json.load(response).get("scope") != "webhook-only":
                raise pa.PrivateAccessError("Unexpected listener")
    except (OSError, ValueError) as exc:
        raise pa.PrivateAccessError("Webhook listener not ready") from exc
    pa.run_cli("funnel", "--yes", "--bg", "--https=8443", TARGET)
    updated = pa.parse_json(pa.run_cli("serve", "status", "--json"))
    if not matches_funnel(updated, hostname):
        raise pa.PrivateAccessError("Unverified Funnel state: inspect and disable 8443 manually")
    return f"Webhooks public at https://{hostname}:8443/hooks/<name>"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    try:
        print(configure(args.disable))
        return 0
    except pa.PrivateAccessError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
