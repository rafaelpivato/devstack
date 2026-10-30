"""Read-only first-setup Tailscale/Funnel readiness checks for the root support timer.

Never perform Tailscale mutations, public network calls, or forward CLI output,
node names, domains, certificate details, identity pins, or credentials.
A healthy local route is NOT evidence of external ChatGPT reachability.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from urllib.request import urlopen

from devstack.mcp_management import MCPManagementError
from devstack.mcp_management import HOST_PATTERN, PIN_FILE

# Enumerated output is validated once more by the snapshot reader.
CONNECTIVITY = frozenset({"connected", "disconnected", "unknown"})
IDENTITY = frozenset({"verified", "not_configured", "changed", "unknown"})
PRIVATE = frozenset({"ready", "migration_required", "missing", "unknown"})
FUNNEL = frozenset({"configured", "disabled", "pending", "unknown"})
GATEWAY = frozenset({"ready", "unavailable", "not_checked"})
HEALTH = frozenset({"ready", "disabled", "attention", "unknown"})
REASONS = frozenset({"none", "snapshot_unavailable", "tailscale_status_unavailable",
    "tailscale_disconnected", "identity_invalid", "identity_not_configured",
    "identity_changed", "identity_unavailable", "serve_status_unavailable",
    "routes_unrecognized", "preferences_unavailable", "private_route_migration",
    "public_route_pending", "gateway_unavailable"})


def unknown() -> dict:
    return {"health": "unknown", "tailscale": "unknown", "identity": "unknown",
            "private_dashboard": "unknown", "public_funnel": "unknown",
            "gateway": "not_checked", "external_reachability": "not_tested",
            "reason": "snapshot_unavailable"}


def _json_status(*args: str) -> dict:
    # Exact known read-only commands; no user arguments or shell.
    if args not in (("tailscale", "status", "--json"),
                    ("tailscale", "serve", "status", "--json")):
        raise ValueError("unsupported_preflight_command")
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=True,
                                timeout=3, stdin=subprocess.DEVNULL)
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("invalid_preflight_data")
        return value
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        raise ValueError("preflight_unavailable") from exc


def _gateway_ready() -> bool:
    try:
        with urlopen("http://127.0.0.1:8768/healthz", timeout=1.5) as response:
            return response.status == 200 and response.headers.get_content_type() == "application/json"
    except (OSError, ValueError, TypeError):
        return False


def capture_preflight(pin_path: Path = PIN_FILE) -> dict:
    """Report local readiness without changing the pinned node or Serve/Funnel.

    The *current* reconciler's exact route classifier is the authority.
    tailscale_layout.identify describes a historical /mcp-/hooks-path topology
    and must NOT classify the reconciler's current public root proxy on :443.
    """
    state = unknown()
    try:
        status = _json_status("tailscale", "status", "--json")
    except ValueError:
        state["reason"] = "tailscale_status_unavailable"
        return state
    if status.get("BackendState") != "Running":
        state.update(tailscale="disconnected", health="attention",
                     reason="tailscale_disconnected")
        return state
    state["tailscale"] = "connected"
    self_node = status.get("Self")
    self_node = self_node if isinstance(self_node, dict) else {}
    host = self_node.get("DNSName")
    if not isinstance(host, str) or not HOST_PATTERN.fullmatch(host.rstrip(".").lower()):
        state.update(health="attention", reason="identity_invalid")
        return state
    host = host.rstrip(".").lower()
    try:
        pin_path = Path(pin_path)
        if pin_path.is_symlink():
            raise ValueError("unsafe_pin")
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state.update(identity="not_configured", reason="identity_not_configured")
    except (OSError, ValueError, TypeError):
        state.update(identity="unknown", reason="identity_unavailable")
    else:
        actual = {"node_id": self_node.get("ID"),
                  "tailnet": (status.get("CurrentTailnet") or {}).get("Name")}
        state["identity"] = "verified" if pin == actual else "changed"
        if state["identity"] != "verified":
            state["reason"] = "identity_changed"

    # The public gateway check is intentionally independent of route matching.
    # On a mismatched Serve config this can still identify a healthy loopback
    # gateway, without ever claiming public Funnel reachability.
    from devstack.networking import read_preferences, classify
    try:
        intent = read_preferences()
    except (OSError, ValueError, TypeError, RuntimeError):
        intent = None
    if intent is not None and intent["mcp"]:
        state["gateway"] = "ready" if _gateway_ready() else "unavailable"

    try:
        routes = _json_status("tailscale", "serve", "status", "--json")
    except ValueError:
        state.update(health="attention", reason="serve_status_unavailable")
        return state
    try:
        phase, _, _ = classify(host, routes)
    except MCPManagementError:
        state.update(health="attention", reason="routes_unrecognized")
        return state

    if phase in ("new", "private_ready", "public_ready"):
        state["private_dashboard"] = "ready"
    elif phase in ("legacy", "dual_private"):
        state["private_dashboard"] = "migration_required"
    else:
        state["private_dashboard"] = "missing"

    if intent is None:
        state["public_funnel"] = "unknown"
        if state["reason"] == "snapshot_unavailable":
            state["reason"] = "preferences_unavailable"
    elif not intent["mcp"]:
        state["public_funnel"] = "disabled"
    elif phase == "new":
        # Current reconciler: public :443 is a root proxy to the unified
        # OAuth-protected MCP/webhook process, not a legacy /mcp handler.
        state["public_funnel"] = "configured"
    else:
        state["public_funnel"] = "pending"

    if state["identity"] != "verified":
        state["health"] = "attention"
    elif state["private_dashboard"] != "ready":
        state["health"] = "attention"
        state["reason"] = "private_route_migration"
    elif state["public_funnel"] == "disabled":
        state.update(health="disabled", reason="none")
    elif state["public_funnel"] == "configured" and state["gateway"] == "ready":
        state.update(health="ready", reason="none")
    elif state["public_funnel"] == "unknown":
        state["health"] = "unknown"
    else:
        state["health"] = "attention"
        state["reason"] = ("gateway_unavailable" if state["gateway"] == "unavailable"
                           else "public_route_pending")
    return state


def validate_preflight(value) -> dict:
    """Never forward arbitrary data from a root-owned JSON snapshot."""
    safe = unknown()
    if not isinstance(value, dict):
        reason = value.get("reason")
    if type(reason) is str and reason in REASONS:
        safe["reason"] = reason
    return safe
    for field, allowed in (("health", HEALTH), ("tailscale", CONNECTIVITY),
                           ("identity", IDENTITY), ("private_dashboard", PRIVATE),
                           ("public_funnel", FUNNEL), ("gateway", GATEWAY)):
        if type(value.get(field)) is str and value[field] in allowed:
            safe[field] = value[field]
    return safe
