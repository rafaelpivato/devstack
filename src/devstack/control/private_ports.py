"""Read-only Tailscale-bound Docker port links for the active project.

This intentionally does not publish any port, proxy requests, reconfigure
Compose, or expose wildcard/loopback bindings as private tailnet URLs.
"""
import ipaddress
import subprocess

from devstack.domain.models import project_id


def tailscale_ipv4() -> str | None:
    try:
        process = subprocess.run(
            ["tailscale", "ip", "-4"], stdin=subprocess.DEVNULL,
            text=True, capture_output=True, check=False, timeout=5,
        )
        if process.returncode:
            return None
        host = process.stdout.strip()
        address = ipaddress.ip_address(host)
        if (address.version != 4 or
                address not in ipaddress.ip_network("100.64.0.0/10")):
            return None
        return host
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


class PrivateApplicationPorts:
    def __init__(self, coordinator, backend):
        self.coordinator = coordinator
        self.backend = backend

    def list(self, identifier: str) -> dict:
        project_id(identifier)
        with self.coordinator.locked():
            self.coordinator.registry.get(identifier)
            state = self.coordinator.status()
            if (state["phase"] != "active" or
                    state["project_id"] != identifier or
                    not state["generation"]):
                return {"project_id": identifier, "active": False,
                        "tailscale_available": False, "ports": [],
                        "applications": []}
            host = tailscale_ipv4()
            if host is None:
                return {"project_id": identifier, "active": True,
                        "tailscale_available": False, "ports": [],
                        "applications": []}
            # Docker port discovery is diagnostic only. Application
            # bookmarks are explicit and never depend on these mappings.
            mappings = self.backend.private_published_ports(
                identifier, state["generation"], host,
            )
            return {"project_id": identifier, "active": True,
                    "tailscale_available": True, "ports": mappings,
                    "applications": []}
