"""Bounded, allowlisted operational events; never store raw log text.

Controller and unprivileged web each write only their own SQLite ring in
separately permissioned state directories. No journal access or user payloads.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import time

SOURCES = frozenset({"controller", "web", "updater"})
CODES = frozenset({
    "controller-started", "controller-stopping", "controller-action", "controller-error",
    "web-error", "web-update-check", "web-update-check-failed",
    "web-update-request", "web-update-rejected",
})
LEVELS = frozenset({"info", "warning", "error"})
WINDOWS = frozenset({1, 5, 30})
MAX_EVENTS = 120
RETAIN = 600


class DiagnosticStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self):
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise RuntimeError("Unsafe diagnostics store")
        if not self.path.parent.is_dir():
            raise RuntimeError("Diagnostics directory unavailable")
        con = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("""CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, at_ns INTEGER NOT NULL,
                source TEXT NOT NULL, level TEXT NOT NULL, code TEXT NOT NULL)""")
            self.path.chmod(0o600)
            return con
        except Exception:
            con.close()
            raise

    def record(self, source: str, code: str, level: str = "info"):
        if source not in SOURCES or code not in CODES or level not in LEVELS:
            raise ValueError("Unrecognized diagnostic event")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO events(at_ns,source,level,code) VALUES(?,?,?,?)",
                        (time.time_ns(), source, level, code))
            con.execute("DELETE FROM events WHERE id NOT IN "
                        "(SELECT id FROM events ORDER BY id DESC LIMIT ?)", (RETAIN,))
            con.execute("COMMIT")

    def snapshot(self, minutes: int) -> dict:
        if type(minutes) is not int or minutes not in WINDOWS:
            raise ValueError("Unsupported diagnostic window")
        cutoff = time.time_ns() - minutes * 60 * 1_000_000_000
        with self._connect() as con:
            rows = con.execute(
                "SELECT at_ns,source,level,code FROM events WHERE at_ns>=? "
                "ORDER BY at_ns DESC,id DESC LIMIT ?", (cutoff, MAX_EVENTS + 1),
            ).fetchall()
        events = [{"at": ns // 1_000_000, "source": source, "level": level,
                   "code": code} for ns, source, level, code in rows[:MAX_EVENTS]]
        return {"events": events, "truncated": len(rows) > MAX_EVENTS}
