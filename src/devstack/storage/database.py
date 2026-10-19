"""SQLite state owned by the v0.5.2 controller, without legacy migration."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 1
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS schema_version (
           singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
           version INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS projects (
           id TEXT PRIMARY KEY,
           slug TEXT NOT NULL UNIQUE,
           name TEXT NOT NULL,
           created_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS repositories (
           id TEXT PRIMARY KEY,
           project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
           name TEXT NOT NULL,
           relative_path TEXT NOT NULL,
           clone_url TEXT NOT NULL,
           created_at INTEGER NOT NULL,
           UNIQUE(project_id, name),
           UNIQUE(project_id, relative_path))""",
    "CREATE INDEX IF NOT EXISTS repositories_by_project ON repositories(project_id)",
)


class StorageError(RuntimeError):
    """Unsafe database path or unrecognized persistence schema."""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise StorageError("Database path must be absolute")
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise StorageError("Database path cannot include symlinks")
        if self.path.exists() and not self.path.is_file():
            raise StorageError("Database path must be a regular file")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        with self.transaction(write=True) as con:
            existing = {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            if existing and "schema_version" not in existing:
                raise StorageError("Unknown database schema; no migration is supported")
            if existing:
                row = con.execute(
                    "SELECT version FROM schema_version WHERE singleton=1"
                ).fetchone()
                if row is None or row[0] != SCHEMA_VERSION:
                    raise StorageError("Unsupported database schema version")
            for sql in _SCHEMA:
                con.execute(sql)
            if not existing:
                con.execute(
                    "INSERT INTO schema_version(singleton,version) VALUES (1,?)",
                    (SCHEMA_VERSION,),
                )
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self, *, write: bool = False):
        try:
            con = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield con
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()
        except sqlite3.DatabaseError as exc:
            raise StorageError("SQLite registry operation failed") from exc
