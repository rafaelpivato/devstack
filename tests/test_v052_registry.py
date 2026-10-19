"""ADR-0001 registry foundation. Integration scenarios AC-01/AC-02 are still pending."""
import concurrent.futures
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from devstack.domain.models import DomainError, project_id, repo_path, slug
from devstack.storage.database import Database, StorageError
from devstack.storage.projects import ProjectRegistry, ProjectConflict, ProjectNotFound


class RegistryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "control" / "devstack.sqlite3"
        self.registry = ProjectRegistry(Database(self.path))

    def test_two_projects_persist_independently(self):
        smoke = self.registry.create("smoke", "Smoke Test")
        beacon = self.registry.create("beacon", "Trusted Outreach")
        self.assertNotEqual(smoke.id, beacon.id)
        self.assertEqual(project_id(smoke.id), smoke.id)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        reopened = ProjectRegistry(Database(self.path))
        self.assertEqual(reopened.list(), (smoke, beacon))
        self.assertEqual(reopened.get(smoke.id), smoke)

    def test_repositories_are_scoped_and_rename_is_not_reidentification(self):
        a = self.registry.create("smoke", "Smoke")
        b = self.registry.create("beacon", "Beacon")
        ar = self.registry.add_repository(
            a.id, "api", "repos/api", "https://github.com/example/a",
        )
        br = self.registry.add_repository(
            b.id, "api", "repos/api", "https://github.com/example/b",
        )
        self.assertNotEqual(ar.id, br.id)
        self.assertEqual(self.registry.repositories(a.id), (ar,))
        self.assertEqual(self.registry.repositories(b.id), (br,))
        renamed = self.registry.rename(a.id, "Renamed Smoke")
        self.assertEqual((renamed.id, renamed.slug), (a.id, "smoke"))
        self.assertEqual(self.registry.repositories(a.id), (ar,))

    def test_creating_projects_does_not_create_runtime_state(self):
        self.registry.create("smoke", "Smoke")
        self.registry.create("beacon", "Beacon")
        with self.registry.database.transaction() as con:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertNotIn("runtime_state", tables)

    def test_duplicate_and_unknown_references_do_not_mutate_other_projects(self):
        a = self.registry.create("smoke", "Smoke")
        with self.assertRaises(ProjectConflict):
            self.registry.create("smoke", "Duplicate")
        with self.assertRaises(ProjectNotFound):
            self.registry.get(str(uuid4()))
        with self.assertRaises(ProjectNotFound):
            self.registry.rename(str(uuid4()), "Missing")
        with self.assertRaises(ProjectNotFound):
            self.registry.add_repository(
                str(uuid4()), "api", "repos/api", "https://github.com/example/r",
            )
        self.registry.add_repository(
            a.id, "api", "repos/api", "https://github.com/example/r",
        )
        for name, path in (("api", "repos/second"), ("other", "repos/api")):
            with self.subTest(name=name, path=path), self.assertRaises(ProjectConflict):
                self.registry.add_repository(
                    a.id, name, path, "https://github.com/example/other",
                )
        self.assertEqual(len(self.registry.repositories(a.id)), 1)
        self.assertEqual(len(self.registry.list()), 1)

    def test_invalid_ids_slugs_and_paths_rejected(self):
        for value in ("A", "../abc", "a_b", "a/", "", "-bad", "a" * 65):
            with self.subTest(value=value), self.assertRaises(DomainError):
                slug(value)
        for value in ("/tmp/escape", "repos/../other", "repos/./api",
                      "repos//api", r"repos\api", ""):
            with self.subTest(value=value), self.assertRaises(DomainError):
                repo_path(value)
        for value in ("smoke", "../etc/passwd", str(uuid4()).upper()):
            with self.subTest(value=value), self.assertRaises(DomainError):
                project_id(value)

    def test_concurrent_writes_have_exactly_one_winner(self):
        def run(_):
            try:
                return ProjectRegistry(Database(self.path)).create(
                    "duplicate", "Parallel project"
                )
            except ProjectConflict:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(run, range(6)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(len(self.registry.list()), 1)

    def test_existing_unknown_schema_rejected_without_removing_its_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite3"
            with sqlite3.connect(path) as con:
                con.execute("CREATE TABLE old_singleton(id TEXT)")
                con.execute("INSERT INTO old_singleton VALUES ('test')")
            with self.assertRaisesRegex(StorageError, "Unknown database schema"):
                Database(path)
            with sqlite3.connect(path) as con:
                self.assertEqual(con.execute("SELECT id FROM old_singleton").fetchone()[0],
                                 "test")

    def test_unsupported_schema_version_refused(self):
        with sqlite3.connect(self.path) as con:
            con.execute("UPDATE schema_version SET version=999")
        with self.assertRaisesRegex(StorageError, "Unsupported database schema"):
            Database(self.path)

    def test_symlink_path_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "db"
            target.write_text("not SQLite")
            link = Path(folder) / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(StorageError, "symlinks"):
                Database(link)


if __name__ == "__main__":
    unittest.main()
