"""Private MCP support snapshot: bounded structured events, never raw journal."""
from __future__ import annotations
import json
from pathlib import Path
import time

DEFAULT_SNAPSHOT = Path("/run/devstack-mcp-diagnostics.json")
WINDOWS = (1, 5, 30)
UPDATER_CODES = frozenset({"updater-started", "updater-exited", "updater-failed"})
WEB_STATES = frozenset({"available", "partial (volatile fallback)", "unavailable"})
UPDATER_STATES = frozenset({"available", "partial (status only)", "unavailable"})
STORAGE_MODES = frozenset({"persistent", "volatile", "unavailable"})
STORAGE_ERRORS = frozenset({"directory_missing", "ownership_mismatch", "permission_denied", "read_only", "sqlite_locked", "sqlite_corrupt", "storage_full", "unsafe_path", "storage_unavailable"})
MAX_BYTES = 50000


def snapshot(minutes: int, path: Path = DEFAULT_SNAPSHOT, *, include_auxiliary: bool = False) -> dict:
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
        result = {"events": events[-120:],
                  "truncated": bool(payload.get("truncated")) or len(events) > 120,
                  "available": payload.get("available") is True}
        if include_auxiliary:
            result["generated_at"] = timestamp
            result["auxiliary"] = _validate_auxiliary(payload.get("auxiliary"), cutoff, now)
            from devstack.funnel_preflight import validate_preflight
            result["network_preflight"] = validate_preflight(payload.get("network_preflight"))
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        result = {"events": [], "truncated": False, "available": False}
        if include_auxiliary:
            result["generated_at"] = None
            result["auxiliary"] = _validate_auxiliary(None, 0, 0)
            from devstack.funnel_preflight import unknown
            result["network_preflight"] = unknown()
        return result


def _validate_auxiliary(payload, cutoff: int, now: int) -> dict:
    """Exact finite fields only: never forward a root-generated arbitrary field."""
    from devstack.diagnostics import CODES, LEVELS
    outputs = {}
    for key, source, allowed_status in (
            ("web", "web", WEB_STATES), ("updater", "updater", UPDATER_STATES)):
        raw = payload.get(key) if isinstance(payload, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        state = raw.get("status")
        state = state if state in allowed_status else "unavailable"
        codes = (frozenset(code for code in CODES if code.startswith("web-"))
                 if source == "web" else UPDATER_CODES)
        selected = []
        for event in raw.get("events", []) if isinstance(raw.get("events"), list) else []:
            if (isinstance(event, dict) and type(event.get("at")) is int
                    and cutoff <= event["at"] <= now + 30_000
                    and event.get("source") == source
                    and event.get("level") in LEVELS and event.get("code") in codes):
                selected.append({key: event[key] for key in ("at", "source", "level", "code")})
        result = {"status": state, "events": selected[-120:],
                  "truncated": raw.get("truncated") is True or len(selected) > 120}
        if source == "web":
            store = raw.get("storage")
            store = store if isinstance(store, dict) else {}
            mode = store.get("mode")
            reason = store.get("error_code")
            if mode not in STORAGE_MODES:
                mode = "unavailable"
            if mode == "persistent" and state != "available":
                mode = "unavailable"
            result["storage"] = {"mode": mode, "error_code":
                None if mode == "persistent" else
                reason if reason in STORAGE_ERRORS else "storage_unavailable"}
        else:
            status = raw.get("updater_status")
            if isinstance(status, dict) and set(status) == {
                    "phase", "unit_state", "last_result", "last_exit_code"}:
                result["updater_status"] = {
                    "phase": status["phase"] if status["phase"] in {
                        "idle", "queued", "updating", "failed"} else "unknown",
                    "unit_state": status["unit_state"] if status["unit_state"] in {
                        "inactive", "active", "activating", "deactivating", "failed", "unknown"} else "unknown",
                    "last_result": status["last_result"] if isinstance(
                        status["last_result"], str) and len(status["last_result"]) <= 40 and
                        all(c.isascii() and (c.isalnum() or c in "_-") for c in status["last_result"]) else "unknown",
                    "last_exit_code": status["last_exit_code"] if isinstance(
                        status["last_exit_code"], str) and len(status["last_exit_code"]) <= 8 and
                        all(c in "0123456789-" for c in status["last_exit_code"]) else "",
                }
            else:
                result["updater_status"] = None
        outputs[source] = result
    return outputs
