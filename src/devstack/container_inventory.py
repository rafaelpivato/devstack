"""A bounded, read-only inventory of Compose-owned containers.

Expose only names, services and health; no Docker env, mounts or secrets.
"""
from __future__ import annotations

import json
import subprocess

from .docker_stacks import DockerError, compose_command, compose_path, project_root


def compose_containers(project: str) -> tuple[dict, ...]:
    if not compose_path(project).is_file():
        return ()
    try:
        process = subprocess.run(
            compose_command(project) + ["ps", "--all", "--format", "json"],
            cwd=project_root(project), capture_output=True,
            text=True, timeout=8, check=False,
        )
        if process.returncode:
            raise DockerError("Could not inspect Docker containers")
        data = process.stdout.strip()
        if not data:
            return ()
        try:
            parsed = json.loads(data)
            rows = parsed if isinstance(parsed, list) else [parsed]
        except ValueError:
            rows = [json.loads(line) for line in data.splitlines() if line.strip()]
        if len(rows) > 100:
            raise DockerError("Too many managed containers to display")
        records = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("Service"), str):
                continue
            records.append({
                "name": str(row.get("Name", ""))[:100],
                "service": row["Service"][:80],
                "state": str(row.get("State", "unknown")).lower()[:30],
                "health": str(row.get("Health") or "")[:30],
            })
        return tuple(sorted(records, key=lambda record: (record["service"], record["name"])))
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        raise DockerError("Container inspection unavailable") from exc
