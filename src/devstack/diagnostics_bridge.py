"""Sanitized cross-service diagnostics captured by the existing root timer.

This module is used by the fixed-purpose MCP diagnostics collector, not by
public MCP, Web HTTP or a controller client. It reads only two named Web
SQLite event rings (read-only) and fixed systemd updater properties. No
arbitrary logs, paths, SQL parameters, commands or project data are accepted.
"""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import re
import sqlite3
import stat
import time

from devstack.diagnostics import CODES, LEVELS, storage_failure_code
from devstack.update_control import update_state, updater_service_events

WEB_PRIMARY = Path("/var/lib/devstack-web/diagnostics.sqlite3")
WEB_FALLBACK = Path("/run/devstack-update-request/web-diagnostics.sqlite3")
WEB_USER = "devstack"
MAX_EVENTS = 120
MAX_DB_SIZE = 32 * 1024 * 1024
UPDATER_CODES = frozenset({"updater-started", "updater-exited", "updater-failed"})


def _read_web(path: Path, owner_uid: int, now_ms: int) -> list[dict]:
    """SQLite URI mode=ro: even running as root, never initialize or mutate."""
    directory = path.parent.lstat()
    metadata = path.lstat()
    if (not stat.S_ISDIR(directory.st_mode)
            or stat.S_ISLNK(directory.st_mode)
            or directory.st_uid != owner_uid
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or metadata.st_size > MAX_DB_SIZE):
        raise RuntimeError("Unsafe Web diagnostics store")
    uri = path.absolute().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.5)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT at_ns,source,level,code FROM events WHERE at_ns>=? "
            "ORDER BY at_ns DESC,id DESC LIMIT ?",
            ((now_ms - 30 * 60 * 1000) * 1_000_000, MAX_EVENTS + 1),
        ).fetchall()
    finally:
        connection.close()
    result = []
    for at_ns, source, level, code in rows:
        if (type(at_ns) is int and (now_ms - 30 * 60 * 1000) <=
                at_ns // 1_000_000 <= now_ms + 30_000 and
                source == "web" and level in LEVELS and code in CODES and
                code.startswith("web-")):
            result.append({"at": at_ns // 1_000_000, "source": "web",
                           "level": level, "code": code})
    return result[:MAX_EVENTS]


def _web_snapshot(primary: Path, fallback: Path, web_user: str, now_ms: int) -> dict:
    try:
        uid = pwd.getpwnam(web_user).pw_uid
    except KeyError:
        return {"status": "unavailable", "storage": {"mode": "unavailable",
                "error_code": "storage_unavailable"}, "events": []}
    primary_events, fallback_events = [], []
    error_code = "storage_unavailable"
    primary_ok = fallback_ok = False
    try:
        primary_events = _read_web(primary, uid, now_ms)
        primary_ok = True
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        error_code = storage_failure_code(exc, primary)
        try:
            if primary.is_file() and primary.stat().st_uid != uid:
                error_code = "ownership_mismatch"
        except OSError:
            pass
    try:
        fallback_events = _read_web(fallback, uid, now_ms)
        fallback_ok = True
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        pass
    merged = sorted(primary_events + fallback_events,
                    key=lambda row: (row["at"], row["code"]))
    return {
        "status": "available" if primary_ok else
                  "partial (volatile fallback)" if fallback_ok else "unavailable",
        "storage": {"mode": "persistent" if primary_ok else
                            "volatile" if fallback_ok else "unavailable",
                    "error_code": None if primary_ok else error_code},
        "events": merged[-MAX_EVENTS:],
        "truncated": len(merged) > MAX_EVENTS,
    }


def _updater_snapshot() -> dict:
    """Safe copies of the Web console's pre-existing systemd show metadata."""
    try:
        data = update_state()
        history = updater_service_events(30)
        phases = {"idle", "queued", "updating", "failed"}
        units = {"inactive", "active", "activating", "deactivating", "failed", "unknown"}
        status = {
            "phase": data.get("phase") if data.get("phase") in phases else "unknown",
            "unit_state": data.get("unit_state") if data.get("unit_state") in units else "unknown",
            "last_result": re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("last_result", "")))[:40],
            "last_exit_code": re.sub(r"[^0-9-]", "", str(data.get("last_exit_code", "")))[:8],
        }
        now = time.time_ns() // 1_000_000
        events = [event for event in history.get("events", [])
                  if (isinstance(event, dict) and
                      event.get("source") == "updater" and
                      event.get("code") in UPDATER_CODES and
                      event.get("level") in LEVELS and
                      type(event.get("at")) is int and
                      now - 30*60*1000 <= event["at"] <= now + 30_000)]
        return {"status": "available" if history.get("available") is True
                else "partial (status only)", "updater_status": status,
                "events": events[:MAX_EVENTS], "truncated": len(events) > MAX_EVENTS}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"status": "unavailable", "updater_status": None, "events": []}


def capture_auxiliary(*, primary: Path = WEB_PRIMARY, fallback: Path = WEB_FALLBACK,
                      web_user: str = WEB_USER) -> dict:
    """Capture an allowlisted 30-minute source snapshot; no arbitrary paths in output."""
    now = time.time_ns() // 1_000_000
    return {
        "web": _web_snapshot(Path(primary), Path(fallback), web_user, now),
        "updater": _updater_snapshot(),
    }
