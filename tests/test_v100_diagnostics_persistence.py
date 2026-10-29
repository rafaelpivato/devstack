"""#100: safe storage classifications and legacy unit migration contracts."""
import errno
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from devstack.diagnostics import DiagnosticStore, storage_failure_code
from devstack.diagnostics_deploy import ensure_state_directory


class StorageFailureTests(unittest.TestCase):
    def test_missing_directory_and_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / "absent" / "events.sqlite3"
            self.assertEqual(storage_failure_code(RuntimeError("secret"), missing),
                             "directory_missing")
            target = root / "actual.sqlite3"
            target.write_bytes(b"")
            link = root / "events.sqlite3"
            link.symlink_to(target)
            self.assertEqual(storage_failure_code(RuntimeError("secret"), link),
                             "unsafe_path")

    def test_sqlite_codes_without_exception_strings(self):
        examples = (
            (sqlite3.SQLITE_BUSY, "sqlite_locked"),
            (sqlite3.SQLITE_CORRUPT, "sqlite_corrupt"),
            (sqlite3.SQLITE_NOTADB, "sqlite_corrupt"),
            (sqlite3.SQLITE_READONLY, "read_only"),
            (sqlite3.SQLITE_PERM, "permission_denied"),
            (sqlite3.SQLITE_FULL, "storage_full"),
        )
        for number, expected in examples:
            with self.subTest(expected=expected):
                exc = sqlite3.OperationalError("Bearer secret /private/path")
                exc.sqlite_errorcode = number
                result = storage_failure_code(exc, Path("/private/path"))
                self.assertEqual(result, expected)
                self.assertNotIn("secret", result)

    def test_os_error_and_ownership_are_safely_classified(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.sqlite3"
            path.write_bytes(b"")
            self.assertEqual(storage_failure_code(OSError(errno.EROFS, "private"), path),
                             "read_only")
            self.assertEqual(storage_failure_code(OSError(errno.EACCES, "secret"), path),
                             "permission_denied")
            with patch("os.geteuid", return_value=os.geteuid()+1):
                self.assertEqual(storage_failure_code(RuntimeError("secret"), path),
                                 "ownership_mismatch")

    def test_sqlite_corrupt_store_is_not_silently_reported_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.sqlite3"
            path.write_bytes(b"invalid sqlite bytes")
            with self.assertRaises(sqlite3.DatabaseError) as caught:
                DiagnosticStore(path).snapshot(5)
            self.assertEqual(storage_failure_code(caught.exception, path), "sqlite_corrupt")


class UnitMigrationTests(unittest.TestCase):
    def test_web_process_cannot_install_systemd_dropins(self):
        with patch("devstack.diagnostics_deploy.os.geteuid", return_value=1001):
            with self.assertRaisesRegex(RuntimeError, "Root required"):
                ensure_state_directory()


if __name__ == "__main__":
    unittest.main()
