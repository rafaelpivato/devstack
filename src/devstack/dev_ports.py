"""Direct development URLs over Tailscale; no HTTP proxy or port publishing.

A link appears only when Docker already binds a registered app's container
port to this machine's exact Tailscale IPv4. Other Compose bindings are ignored.
"""
from __future__ import annotations

import ipaddress
import json
import subprocess

from .docker_stacks import DockerError, compose_command, compose_path, project_root


def tailscale_ipv4() -> str | None:
    try:
        result = subprocess.run(["tailscale", "ip", "-4"], text=True,
                                capture_output=True, check=False, timeout=4)
        if result.returncode:
            return None
        value = result.stdout.strip()
        address = ipaddress.ip_address(value)
        # Tailscale currently allocates the shared 100.64.0.0/10 CGNAT block.
        # Never construct URLs from arbitrary host environment variables.
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


def application_urls(project: str, apps: tuple) -> dict[str, str]:
    """Only explicitly published *tailnet-bound* host TCP ports get links."""
    host = tailscale_ipv4()
    if host is None or not apps:
        return {}
    result = {}
    for row in _rows(project):
        if str(row.get("State", "")).lower() != "running":
            continue
        service = row.get("Service")
        publishers = row.get("Publishers")
        if not isinstance(publishers, list):
            continue
        for app in apps:
            if app.service != service or app.port is None:
                continue
            for item in publishers:
                if not isinstance(item, dict):
                    continue
                if (item.get("URL") != host
                        or item.get("Protocol", "").lower() != "tcp"):
                    continue
                try:
                    target = int(item.get("TargetPort"))
                    published = int(item.get("PublishedPort"))
                except (TypeError, ValueError):
                    continue
                if target == app.port and 1 <= published <= 65535:
                    result[app.name] = f"http://{host}:{published}/"
    return result
