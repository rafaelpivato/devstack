"""Discover private development ports from Docker Compose published mappings.

No static DevStack development ports, proxy, or firewall rewriting. Only
running Compose services published on the host's *exact* Tailscale IPv4 may
appear in private previews. A wildcard binding is never advertised.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import subprocess

from .docker_stacks import DockerError, compose_command, compose_path, project_root
from . import runtime


@dataclass(frozen=True)
class PublishedPort:
    service: str
    container_port: int
    host_port: int
    url: str


def tailscale_ipv4() -> str | None:
    try:
        result = subprocess.run(["tailscale", "ip", "-4"], text=True,
                                capture_output=True, check=False, timeout=4)
        if result.returncode:
            return None
        value = result.stdout.strip()
        address = ipaddress.ip_address(value)
        # CGNAT tailnet address, not an arbitrary local/public interface.
        if address not in ipaddress.ip_network("100.64.0.0/10"):
            return None
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _rows(project: str) -> list[dict]:
    try:
        if not compose_path(project).is_file():
            return []
        cmd = compose_command(project) + ["ps", "--all", "--format", "json"]
        result = subprocess.run(cmd, cwd=project_root(project), text=True,
                                capture_output=True, timeout=7, check=False)
        if result.returncode or not result.stdout.strip():
            return []
        try:
            parsed = json.loads(result.stdout)
            rows = parsed if isinstance(parsed, list) else [parsed]
        except ValueError:
            rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return [row for row in rows if isinstance(row, dict)]
    except (DockerError, OSError, ValueError, subprocess.TimeoutExpired):
        return []


def published_ports(project: str) -> tuple[PublishedPort, ...]:
    """Enumerate only the currently running project's private Compose ports."""
    if not runtime.is_active(project):
        return ()
    host = tailscale_ipv4()
    if host is None:
        return ()
    found: set[PublishedPort] = set()
    for row in _rows(project):
        if str(row.get("State", "")).lower() != "running":
            continue
        service = row.get("Service")
        if not isinstance(service, str) or not service:
            continue
        publishers = row.get("Publishers")
        if not isinstance(publishers, list):
            continue
        for item in publishers:
            if not isinstance(item, dict):
                continue
            if (item.get("URL") != host or
                    str(item.get("Protocol", "")).lower() != "tcp"):
                continue
            try:
                target = int(item["TargetPort"])
                published = int(item["PublishedPort"])
                if not (1 <= target <= 65535 and 1 <= published <= 65535):
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            found.add(PublishedPort(service, target, published,
                                    f"http://{host}:{published}/"))
    return tuple(sorted(found, key=lambda p: (p.service, p.host_port, p.container_port)))


def application_urls(project: str, apps: tuple) -> dict[str, str]:
    """Map registered display labels to Compose-selected host URLs."""
    if not apps:
        return {}
    mappings = published_ports(project)
    result = {}
    for app in apps:
        # Optional app.port narrows the matching target port; when absent,
        # Compose's port mapping remains the authority.
        candidates = [entry for entry in mappings if entry.service == app.service
                      and (app.port is None or app.port == entry.container_port)]
        if candidates:
            result[app.name] = candidates[0].url
    return result
