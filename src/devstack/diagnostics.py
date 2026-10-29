"""Bounded, allowlisted operational events; never store raw log text.

Controller and unprivileged web each write only their own SQLite ring in
separately permissioned state directories. No journal access or user payloads.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time

SOURCES = frozenset({"controller", "web", "updater"})
CODES = frozenset({
    "controller-started", "controller-stopping", "controller-action", "controller-error",
    "runtime-request", "runtime-reconcile", "project-create", "project-import",
    "repository-update", "repository-add", "repository-bulk-update", "repository-branch",
    "compose-operation", "job-submit",
    "web-started", "web-error", "web-update-check", "web-update-check-failed",
    "web-update-request", "web-update-rejected",
    "web-mcp-enable-requested", "web-mcp-disable-requested", "web-mcp-request-failed",
})
LEVELS = frozenset({"info", "warning", "error"})
WINDOWS = frozenset({1, 5, 30})
MAX_EVENTS = 120
RETAIN = 600


class DiagnosticStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise RuntimeError("Unsafe diagnostics store")
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise RuntimeError("Diagnostics directory unavailable")
        con = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("""CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, at_ns INTEGER NOT NULL,
                source TEXT NOT NULL, level TEXT NOT NULL, code TEXT NOT NULL)""")
            self.path.chmod(0o600)
            yield con
            if con.in_transaction:
                con.commit()
        finally:
            con.close()

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


# Public diagnostics expose only these static categories, never exceptions,
# path names or host output. All probing happens under the Web service UID.
def storage_failure_code(exc: Exception, path: Path) -> str:
    import errno
    import os
    import stat

    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return "sqlite_locked"
        if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            return "sqlite_corrupt"
        if code == sqlite3.SQLITE_READONLY:
            return "read_only"
        if code == sqlite3.SQLITE_PERM:
            return "permission_denied"
        if code == sqlite3.SQLITE_FULL:
            return "storage_full"
    if isinstance(exc, OSError):
        if exc.errno == errno.EROFS:
            return "read_only"
        if exc.errno in (errno.EACCES, errno.EPERM):
            return "permission_denied"
        if exc.errno == errno.ENOSPC:
            return "storage_full"

    path = Path(path)
    for candidate, is_dir in ((path.parent, True), (path, False)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return "directory_missing" if is_dir else "storage_unavailable"
        except PermissionError:
            return "permission_denied"
        except OSError:
            return "storage_unavailable"
        if stat.S_ISLNK(info.st_mode):
            return "unsafe_path"
        if not (stat.S_ISDIR(info.st_mode) if is_dir
                else stat.S_ISREG(info.st_mode)):
            return "unsafe_path"
        if info.st_uid != os.geteuid():
            return "ownership_mismatch"
        if not os.access(candidate, os.W_OK | (os.X_OK if is_dir else os.R_OK)):
            return "permission_denied"
    try:
        if os.statvfs(path.parent).f_flag & getattr(os, "ST_RDONLY", 1):
            return "read_only"
    except OSError:
        pass
    return "storage_unavailable"
