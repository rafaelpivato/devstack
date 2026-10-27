#!/usr/bin/env python3
"""Explicit, guarded public webhook publication using shared HTTPS :443.

MCP and webhooks share *routes*, not an untrusted root handler. Legacy 8443
webhooks can only be disabled until the Web-first port migration is complete.
"""
from __future__ import annotations
import argparse
import json
import sys
from urllib import request
import private_access as pa

TARGET = "http://127.0.0.1:8767"
PUBLIC_TARGET = TARGET + "/hooks/"
MCP_TARGET = "http://127.0.0.1:8768/mcp"
MCP_LEGACY = "http://127.0.0.1:8768"


def matches_legacy(config, hostname, *, hooks=False, mcp=False):
    tcp = {"443": {"HTTPS": True}}
    web = {hostname + ":443": {"Handlers": {"/": {"Proxy": pa.LOCAL_TARGET}}}}
    allow = {}
    if hooks:
        tcp["8443"] = {"HTTPS": True}
        web[hostname + ":8443"] = {"Handlers": {"/": {"Proxy": TARGET}}}
        allow[hostname + ":8443"] = True
    if mcp:
        tcp["10000"] = {"HTTPS": True}
        web[hostname + ":10000"] = {"Handlers": {"/": {"Proxy": MCP_LEGACY}}}
        allow[hostname + ":10000"] = True
    return (config.get("TCP") or {}) == tcp and (config.get("Web") or {}) == web and (
        config.get("AllowFunnel") or {}) == allow and not set(config) - {"TCP", "Web", "AllowFunnel"}


def matches_new(config, hostname, *, hooks=False, mcp=False):
    tcp = {"8443": {"HTTPS": True}}
    web = {hostname + ":8443": {"Handlers": {"/": {"Proxy": pa.LOCAL_TARGET}}}}
    allow = {}
    handlers = {}
    if hooks:
        handlers["/hooks/"] = {"Proxy": PUBLIC_TARGET}
    if mcp:
        handlers["/mcp"] = {"Proxy": MCP_TARGET}
    if handlers:
        tcp["443"] = {"HTTPS": True}
        web[hostname + ":443"] = {"Handlers": handlers}
        allow[hostname + ":443"] = True
    return (config.get("TCP") or {}) == tcp and (config.get("Web") or {}) == web and (
        config.get("AllowFunnel") or {}) == allow and not set(config) - {"TCP", "Web", "AllowFunnel"}


def matches_funnel(config, hostname):
    """Compatibility with old helper tests and legacy-only configurations."""
    return matches_legacy(config, hostname, hooks=True)


def read_config():
    return pa.parse_json(pa.run_cli("serve", "status", "--json"))


def verify_listener():
    try:
        with request.urlopen("http://127.0.0.1:8767/healthz", timeout=5) as response:
            if json.load(response).get("scope") != "webhook-only":
                raise pa.PrivateAccessError("Unexpected webhook-only listener")
    except (OSError, ValueError) as exc:
        raise pa.PrivateAccessError("Webhook listener not ready") from exc


def configure(disable=False):
    status = pa.parse_json(pa.run_cli("status", "--json"))
    hostname = pa.connected_hostname(status)
    pa.pin_initial_identity(status)
    current = read_config()
    mode = None
    hooks = mcp = False
    for has_mcp in (False, True):
        for has_hooks in (False, True):
            if matches_legacy(current, hostname, hooks=has_hooks, mcp=has_mcp):
                mode, hooks, mcp = "legacy", has_hooks, has_mcp
            if matches_new(current, hostname, hooks=has_hooks, mcp=has_mcp):
                mode, hooks, mcp = "new", has_hooks, has_mcp
    if mode is None:
        raise pa.PrivateAccessError("Unknown Serve/Funnel state; no changes made")
    if disable:
        if not hooks:
            return "Webhook Funnel already disabled"
        if mode == "legacy":
            pa.run_cli("funnel", "--https=8443", "off")
        else:
            pa.run_cli("funnel", "--https=443", "--set-path=/hooks/", "off")
        updated = read_config()
        verified = (matches_legacy(updated, hostname, hooks=False, mcp=mcp)
                    if mode == "legacy" else
                    matches_new(updated, hostname, hooks=False, mcp=mcp))
        if not verified:
            raise pa.PrivateAccessError("Unable to verify removal of only the webhook route")
        return "Webhook Funnel disabled; other managed routes preserved"
    if mode == "legacy":
        # No more newly published non-standard ports. Legacy callbacks must be
        # disabled before the explicit Web-first :443/:8443 cutover.
        if hooks:
            return f"Legacy webhook URL: https://{hostname}:8443/hooks/<project-uuid>/<route>"
        raise pa.PrivateAccessError("Migrate private dashboard to 8443 in the Web UI first")
    if hooks:
        return f"Webhooks public at https://{hostname}/hooks/<project-uuid>/<route>"
    verify_listener()
    pa.run_cli("funnel", "--yes", "--bg", "--https=443",
               "--set-path=/hooks/", PUBLIC_TARGET)
    updated = read_config()
    if not matches_new(updated, hostname, hooks=True, mcp=mcp):
        raise pa.PrivateAccessError("Public webhook route not verified; check Web diagnostics")
    return f"Webhooks public at https://{hostname}/hooks/<project-uuid>/<route>"


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
