"""Strict Tailscale port topology for the DevStack public/private cutover.

Every accepted map is *exact*, including disabled routes. No generic Serve
configuration, arbitrary backend, wildcard path, or public dashboard is allowed.
"""
from dataclasses import dataclass

DASHBOARD = "http://127.0.0.1:8765"
WEBHOOK_OLD = "http://127.0.0.1:8767"
MCP_OLD = "http://127.0.0.1:8768"
# Tailscale removes the mount prefix before proxying. Restore backend paths.
WEBHOOK_PUBLIC = "http://127.0.0.1:8767/hooks/"
MCP_PUBLIC = "http://127.0.0.1:8768/mcp"


@dataclass(frozen=True)
class Topology:
    phase: str
    webhooks: bool
    mcp: bool


def config(host: str, *, private: str = "443", webhooks: bool = False,
           mcp: bool = False, old_mcp: bool = False,
           private_443: bool = False) -> dict:
    """Return a single allowlisted topology (only used for validation/tests)."""
    tcp: dict[str, dict] = {}
    web: dict[str, dict] = {}
    funnel: dict[str, bool] = {}
    if private in {"443", "8443"}:
        tcp[private] = {"HTTPS": True}
        web[f"{host}:{private}"] = {"Handlers": {"/": {"Proxy": DASHBOARD}}}
    if private_443:
        tcp["443"] = {"HTTPS": True}
        web[f"{host}:443"] = {"Handlers": {"/": {"Proxy": DASHBOARD}}}
    if private == "443":
        if webhooks:
            tcp["8443"] = {"HTTPS": True}
            web[f"{host}:8443"] = {"Handlers": {"/": {"Proxy": WEBHOOK_OLD}}}
            funnel[f"{host}:8443"] = True
        if mcp:
            tcp["10000"] = {"HTTPS": True}
            web[f"{host}:10000"] = {"Handlers": {"/": {"Proxy": MCP_OLD}}}
            funnel[f"{host}:10000"] = True
    else:
        handlers = {}
        if mcp:
            handlers["/mcp"] = {"Proxy": MCP_PUBLIC}
        if webhooks:
            handlers["/hooks/"] = {"Proxy": WEBHOOK_PUBLIC}
        if handlers:
            tcp["443"] = {"HTTPS": True}
            web[f"{host}:443"] = {"Handlers": handlers}
            funnel[f"{host}:443"] = True
        if old_mcp:
            tcp["10000"] = {"HTTPS": True}
            web[f"{host}:10000"] = {"Handlers": {"/": {"Proxy": MCP_OLD}}}
            funnel[f"{host}:10000"] = True
    return {"TCP": tcp, "Web": web, "AllowFunnel": funnel}


def identify(host: str, state: dict) -> Topology:
    """Match only known, isolated states; explicitly include cutover checkpoints."""
    if not isinstance(state, dict) or set(state) - {"TCP", "Web", "AllowFunnel"}:
        raise ValueError("unknown_tailscale_routes")
    normalized = {k: state.get(k) or {} for k in ("TCP", "Web", "AllowFunnel")}
    if any(not isinstance(v, dict) for v in normalized.values()):
        raise ValueError("unknown_tailscale_routes")
    for hooks in (False, True):
        for mcp in (False, True):
            if normalized == config(host, webhooks=hooks, mcp=mcp):
                return Topology("legacy", hooks, mcp)
            if normalized == config(host, private="8443", webhooks=hooks, mcp=mcp):
                return Topology("new", hooks, mcp)
    # Transitional routes do not enable webhooks: old callback URLs would change.
    for old_mcp in (False, True):
        if normalized == config(host, private="8443", private_443=True,
                                old_mcp=old_mcp):
            return Topology("dual_private", False, old_mcp)
        if normalized == config(host, private="8443", old_mcp=old_mcp):
            return Topology("private_ready" if old_mcp else "new", False, old_mcp)
    if normalized == config(host, private="8443", mcp=True, old_mcp=True):
        return Topology("public_ready", False, True)
    raise ValueError("unknown_tailscale_routes")
