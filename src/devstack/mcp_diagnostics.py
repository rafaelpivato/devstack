"""Private MCP support snapshot: bounded structured events, never raw journal."""
from __future__ import annotations
import json
from pathlib import Path
import time

DEFAULT_SNAPSHOT = Path("/run/devstack-mcp-diagnostics.json")
WINDOWS = (1, 5, 30)
MAX_BYTES = 50000


def snapshot(minutes: int, path: Path = DEFAULT_SNAPSHOT) -> dict:
    if type(minutes) is not int or minutes not in WINDOWS:
        raise ValueError("Unsupported diagnostic window")
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise OSError("MCP snapshot missing or oversized")
        payload = json.loads(path.read_text(encoding="utf-8"))
        timestamp = payload["generated_at"]
        now = time.time_ns() // 1_000_000
        if (type(timestamp) is not int or timestamp > now + 30_000 or
                now - timestamp > 150_000):
            raise ValueError("Stale MCP diagnostics")
        events = []
        cutoff = now - minutes * 60 * 1000
        for item in payload["events"]:
            if not isinstance(item, dict) or set(item) != {"at", "source", "level", "code"}:
                continue
            at, code = item["at"], item["code"]
            if (type(at) is int and cutoff <= at <= now + 30_000 and
                item["source"] == "mcp" and item["level"] in ("info", "error", "warning")
                and isinstance(code, str) and len(code) <= 70 and
                code.startswith("mcp-") and
                all(c in "abcdefghijklmnopqrstuvwxyz-" for c in code)):
                events.append(item)
        return {"events": events[-120:],
                "truncated": bool(payload.get("truncated")) or len(events) > 120,
                "available": payload.get("available") is True}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        return {"events": [], "truncated": False, "available": False}
