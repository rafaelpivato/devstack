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

from devstack.tailscale_layout import identify
from devstack.mcp_management import HOST_PATTERN, PIN_FILE

# Enumerated output is validated once more by the snapshot reader.
CONNECTIVITY = frozenset({"connected", "disconnected", "unknown"})
IDENTITY = frozenset({"verified", "not_configured", "changed", "unknown"})
PRIVATE = frozenset({"ready", "migration_required", "missing", "unknown"})
FUNNEL = frozenset({"configured", "disabled", "pending", "unknown"})
GATEWAY = frozenset({"ready", "unavailable", "not_checked"})
HEALTH = frozenset({"ready", "disabled", "attention", "unknown"})


def unknown() -> dict:
    return {"health": "unknown", "tailscale": "unknown", "identity": "unknown",
            "private_dashboard": "unknown", "public_funnel": "unknown",
            "gateway": "not_checked", "external_reachability": "not_tested"}


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
    """No changes to node, Serve/Funnel or enabled integrations."""
    state = unknown()
    try:
        status = _json_status("tailscale", "status", "--json")
    except ValueError:
        return state
    if status.get("BackendState") != "Running":
        state.update(tailscale="disconnected", health="attention")
        return state
    state["tailscale"] = "connected"
    self_node = status.get("Self")
    self_node = self_node if isinstance(self_node, dict) else {}
    host = self_node.get("DNSName")
    if not isinstance(host, str) or not HOST_PATTERN.fullmatch(host.rstrip(".").lower()):
        state["health"] = "attention"
        return state
    host = host.rstrip(".").lower()
    try:
        pin_path = Path(pin_path)
        if pin_path.is_symlink():
            raise ValueError("unsafe_pin")
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state["identity"] = "not_configured"
    except (OSError, ValueError, TypeError):
        state["identity"] = "unknown"
    else:
        actual = {"node_id": self_node.get("ID"),
                  "tailnet": (status.get("CurrentTailnet") or {}).get("Name")}
        state["identity"] = "verified" if pin == actual else "changed"
    try:
        topology = identify(host, _json_status("tailscale", "serve", "status", "--json"))
    except (ValueError, AttributeError, KeyError, TypeError):
        state["health"] = "attention"
        return state
    if topology.phase in ("new", "private_ready", "public_ready"):
        state["private_dashboard"] = "ready"
    elif topology.phase in ("legacy", "dual_private"):
        state["private_dashboard"] = "migration_required"
    else:
        state["private_dashboard"] = "missing"
    try:
        from devstack.networking import read_preferences
        intent = read_preferences()
    except (OSError, ValueError, TypeError, RuntimeError):
        intent = None
    if intent is None:
        state["public_funnel"] = "unknown"
    elif not intent["mcp"]:
        state["public_funnel"] = "disabled"
    elif topology.phase == "new" and topology.mcp:
        state["public_funnel"] = "configured"
    else:
        state["public_funnel"] = "pending"
    if state["public_funnel"] == "configured":
        state["gateway"] = "ready" if _gateway_ready() else "unavailable"
    if state["identity"] != "verified" or state["private_dashboard"] != "ready":
        state["health"] = "attention"
    elif state["public_funnel"] == "disabled":
        state["health"] = "disabled"
    elif state["public_funnel"] == "configured" and state["gateway"] == "ready":
        state["health"] = "ready"
    elif state["public_funnel"] == "unknown":
        state["health"] = "unknown"
    else:
        state["health"] = "attention"
    return state


def validate_preflight(value) -> dict:
    """Never forward arbitrary data from a root-owned JSON snapshot."""
    safe = unknown()
    if not isinstance(value, dict):
        return safe
    for field, allowed in (("health", HEALTH), ("tailscale", CONNECTIVITY),
                           ("identity", IDENTITY), ("private_dashboard", PRIVATE),
                           ("public_funnel", FUNNEL), ("gateway", GATEWAY)):
        if type(value.get(field)) is str and value[field] in allowed:
            safe[field] = value[field]
    return safe
