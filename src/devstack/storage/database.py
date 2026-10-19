"""SQLite state owned by the v0.5.2 controller, without legacy migration."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 9
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS schema_version (
           singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
           version INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS projects (
           id TEXT PRIMARY KEY,
           slug TEXT NOT NULL UNIQUE,
           name TEXT NOT NULL,
           created_at INTEGER NOT NULL,
           status TEXT NOT NULL DEFAULT 'ready'
             CHECK(status IN ('ready','pending')))""",
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
    """CREATE TABLE IF NOT EXISTS project_sources (
           project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
           repository_id TEXT NOT NULL REFERENCES repositories(id),
           subdirectory TEXT NOT NULL,
           branch TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS applications (
           project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
           name TEXT NOT NULL,
           service TEXT NOT NULL,
           port INTEGER,
           PRIMARY KEY(project_id,name))""",
    """CREATE TABLE IF NOT EXISTS runtime_state (
           singleton INTEGER PRIMARY KEY CHECK(singleton=1),
           phase TEXT NOT NULL CHECK(phase IN
             ('inactive','activating','active','deactivating','switching','error')),
           project_id TEXT,
           generation TEXT,
           message TEXT NOT NULL DEFAULT '',
           updated_ns INTEGER NOT NULL,
           FOREIGN KEY(project_id) REFERENCES projects(id))""",
    """CREATE TABLE IF NOT EXISTS jobs (
           id TEXT PRIMARY KEY,
           project_id TEXT NOT NULL REFERENCES projects(id),
           generation TEXT NOT NULL,
           kind TEXT NOT NULL CHECK(kind IN ('task','compose','adhoc')),
           status TEXT NOT NULL CHECK(status IN
             ('queued','running','cancelling','succeeded','failed','cancelled')),
           action TEXT NOT NULL,
           cancel_policy TEXT NOT NULL DEFAULT 'project' CHECK(cancel_policy IN ('independent','project')),
           started_ns INTEGER NOT NULL,
           finished_ns INTEGER,
           exit_code INTEGER)""",
    """CREATE TABLE IF NOT EXISTS import_operations (
           id TEXT PRIMARY KEY,
           request_key TEXT NOT NULL UNIQUE,
           request_digest TEXT NOT NULL,
           url TEXT NOT NULL,
           slug TEXT NOT NULL,
           name TEXT NOT NULL,
           target_id TEXT NOT NULL UNIQUE,
           status TEXT NOT NULL CHECK(status IN
             ('queued','running','succeeded','failed')),
           project_id TEXT REFERENCES projects(id),
           error_code TEXT NOT NULL DEFAULT '',
           started_ns INTEGER NOT NULL,
           finished_ns INTEGER)""",
    """CREATE TABLE IF NOT EXISTS webhook_routes (
           project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
           route TEXT NOT NULL,
           service TEXT NOT NULL,
           port INTEGER NOT NULL CHECK(port BETWEEN 1 AND 65535),
           destination TEXT NOT NULL,
           PRIMARY KEY(project_id,route))""",
    """CREATE TABLE IF NOT EXISTS webhook_leases (
           id TEXT PRIMARY KEY,
           project_id TEXT NOT NULL REFERENCES projects(id),
           generation TEXT NOT NULL,
           started_ns INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS repository_add_operations (
           id TEXT PRIMARY KEY,
           project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
           request_key TEXT NOT NULL,
           request_digest TEXT NOT NULL,
           repo_id TEXT NOT NULL UNIQUE,
           name TEXT NOT NULL,
           clone_url TEXT NOT NULL,
           branch TEXT NOT NULL,
           status TEXT NOT NULL CHECK(status IN
             ('queued','running','recovery','succeeded','failed')),
           error_code TEXT NOT NULL DEFAULT '',
           started_ns INTEGER NOT NULL,
           finished_ns INTEGER,
           UNIQUE(project_id,request_key))""",
    "CREATE INDEX IF NOT EXISTS pending_repository_adds ON repository_add_operations(project_id,name,status)",
    """CREATE TABLE IF NOT EXISTS source_sync_pending (
           project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE RESTRICT,
           payload TEXT NOT NULL,
           started_ns INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS tasks (
           project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
           name TEXT NOT NULL,
           service TEXT NOT NULL,
           mode TEXT NOT NULL,
           image TEXT NOT NULL,
           network TEXT NOT NULL,
           argv_json TEXT NOT NULL,
           PRIMARY KEY(project_id,name))""",
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
                if not {"projects", "repositories", "project_sources",
                        "applications", "tasks", "runtime_state", "jobs", "import_operations", "webhook_routes", "webhook_leases", "source_sync_pending", "repository_add_operations"}.issubset(existing):
                    raise StorageError("Database schema is incomplete")
            for sql in _SCHEMA:
                con.execute(sql)
            if not existing:
                con.execute(
                    "INSERT INTO runtime_state"
                    "(singleton,phase,project_id,generation,updated_ns)"
                    " VALUES(1,'inactive',NULL,NULL,0)"
                )
                con.execute(
                    "INSERT INTO schema_version(singleton,version) VALUES (1,?)",
                    (SCHEMA_VERSION,),
                )
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self, *, write: bool = False):
        con = None
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
        except sqlite3.IntegrityError:
            # Domain registry translates UNIQUE and FK violations itself.
            raise
        except sqlite3.DatabaseError as exc:
            raise StorageError("SQLite registry operation failed") from exc
        finally:
            if con is not None:
                con.close()
