"""Explicit bookmark URLs, live upgrade of old project registrations."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from devstack.projects.manifest import ManifestError, _apps, application_row
from devstack.storage.database import Database, SCHEMA_VERSION
from devstack.storage.projects import ProjectRegistry
from devstack.projects.workspaces import WorkspaceStore
from devstack.control.daemon import Controller


class FakeDocker:
    def owned(self):
        return []


class ApplicationURLManifestTests(unittest.TestCase):
    def test_multiple_bookmarks_can_share_port_without_service(self):
        config = (
            '[[applications]]\nname="Frontend"\n'
            'url="http://{host}:5173/a/login"\n'
            '[[applications]]\nname="Django Admin"\n'
            'url="http://{host}:5173/b/admin/"\n'
        ).encode()
        apps = _apps(config)
        self.assertEqual([a.url for a in apps], [
            "http://{host}:5173/a/login", "http://{host}:5173/b/admin/",
        ])
        self.assertEqual([a.service for a in apps], ["", ""])
        self.assertEqual([application_row(a.name,a.service,a.port,a.url) for a in apps],
                         [{"name":"Frontend","url":apps[0].url},
                          {"name":"Django Admin","url":apps[1].url}])

    def test_explicit_urls_reject_unsafe_schemes_host_placeholders_and_credentials(self):
        invalid = (
            "javascript:alert(1)", "file:///etc/passwd",
            "http://user:password@localhost:8000/x",
            "http://{host}.evil:8000/a",
            "http://{host}:{host}/a",
            "http://localhost:70000/a",
            "http://localhost/a?token=secret",
            "http://localhost/a#fragment",
            "http://localhost\\@evil/a",
        )
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ManifestError):
                _apps(('[['+'applications'+']]\nname="One"\nurl='+repr(url)+'\n').encode())
        with self.assertRaises(ManifestError):
            _apps(b'[[applications]]\nname="One"\nservice="api"\nurl="http://x/"')
        with self.assertRaises(ManifestError):
            _apps(b'[[applications]]\nname="One"\nurl="/a/login"')

    def test_legacy_manifest_readable_but_without_generated_url(self):
        app = _apps(b'[[applications]]\nname="Old"\nservice="web"\nport=8080')[0]
        self.assertIsNone(app.url)
        self.assertEqual(application_row(app.name,app.service,app.port,app.url),
                         {"name":"Old","service":"web","port":8080})


class ApplicationURLPersistenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.controller = Controller(
            Database(self.root / "controller.sqlite3"),
            WorkspaceStore(self.root / "projects"), FakeDocker(),
        )
        self.controller.coordinator.reconcile()
        self.id = self.controller.provisioner.create_empty("bookmarks", "Bookmarks").id

    def test_replace_and_read_exact_urls_independent_of_service_or_docker(self):
        apps = [
            {"name":"Frontend","url":"http://{host}:5173/a/login"},
            {"name":"Django Admin","url":"http://{host}:5173/b/admin/"},
        ]
        actual = self.controller.configuration.replace_settings(self.id,apps,[])
        expected = sorted(apps, key=lambda app: app["name"])
        self.assertEqual(actual["applications"], expected)
        detail = self.controller.handle({"op":"project_detail","project_id":self.id})
        self.assertEqual(detail["applications"], expected)
        self.assertEqual(
            self.controller.configuration.settings_review(self.id)["applications"],
            expected,
        )
        with self.assertRaises(Exception):
            self.controller.configuration.replace_settings(
                self.id,[{"name":"unsafe","url":"javascript:alert(1)"}],[])
        self.assertEqual(self.controller.configuration.settings(self.id)["applications"], expected)

    def test_upgrade_v12_preserves_old_apps_and_every_other_project_record(self):
        path=self.root / "controller.sqlite3"
        with sqlite3.connect(path) as db:
            # Model the exact pre-upgrade table shape without the URL column.
            db.execute("DROP TABLE applications")
            db.execute("""CREATE TABLE applications (
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL, service TEXT NOT NULL, port INTEGER,
                PRIMARY KEY(project_id,name))""")
            db.execute("INSERT INTO applications VALUES(?,?,?,?)",
                       (self.id,"Existing","web",8000))
            db.execute("UPDATE schema_version SET version=12")
        upgraded=Database(path)
        self.assertEqual(SCHEMA_VERSION,13)
        with upgraded.transaction() as con:
            self.assertEqual(con.execute("SELECT version FROM schema_version").fetchone()[0],13)
            row=con.execute("SELECT name,service,port,url FROM applications").fetchone()
            self.assertEqual(tuple(row),("Existing","web",8000,None))
            self.assertIsNotNone(con.execute("SELECT id FROM projects WHERE id=?",(self.id,)).fetchone())
        # Idempotent startup after migration does not change data.
        Database(path)
        self.assertEqual(ProjectRegistry(upgraded).list()[0].id,self.id)


if __name__=="__main__":
    unittest.main()
