"""ADR-0001 Stage 2: GitHub template onboarding to two independent workspaces.

These tests exercise Stage 2 via an injected fake GitHub adapter. Real GitHub
network access, Docker and the project-scoped HTTP API are later integration
tests, not falsely counted as complete by this unit suite.
"""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from devstack.adapters.github import (
    GitHubCheckout, GitHubSourceError, parse_github_url, discover_manifest,
)
from devstack.projects.manifest import ManifestError, parse_template
from devstack.projects.service import ProjectProvisioner
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database
from devstack.storage.projects import ProjectConflict, ProjectRegistry


SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "smoke"
URL = "https://github.com/rafaelpivato/devstack/tree/main/examples/smoke"


class FakeGitHub:
    def clone_and_resolve(self, url, destination):
        source = parse_github_url(url)
        destination.mkdir()
        target = destination / "examples" / "smoke"
        target.parent.mkdir()
        shutil.copytree(SAMPLE, target)
        return GitHubCheckout(source.clone_url, source.repository, "main",
                              "examples/smoke", target)


class ProjectProvisionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.registry = ProjectRegistry(Database(self.base / "db" / "control.sqlite3"))
        self.workspaces = WorkspaceStore(self.base / "workspaces")
        self.service = ProjectProvisioner(self.registry, self.workspaces, FakeGitHub())

    def test_import_same_github_template_twice_with_independent_projects(self):
        a = self.service.import_github(URL, "smoke-a", "Smoke A")
        b = self.service.import_github(URL, "smoke-b", "Smoke B")
        self.assertNotEqual(a.id, b.id)
        self.assertEqual({entry.id for entry in self.registry.list()}, {a.id, b.id})
        self.assertEqual(len(self.registry.repositories(a.id)), 1)
        self.assertEqual(len(self.registry.repositories(b.id)), 1)
        self.assertNotEqual(self.workspaces.path(a.id), self.workspaces.path(b.id))
        self.assertEqual((self.workspaces.path(a.id) / "managed" / "tasks.toml").read_bytes(),
                         (SAMPLE / ".devstack" / "tasks.toml").read_bytes())
        self.assertEqual((self.workspaces.path(b.id) / "managed" / "compose.yaml").read_bytes(),
                         (SAMPLE / ".devstack" / "compose.yaml").read_bytes())
        self.assertEqual(
            self.workspaces.repository(a.id, "repos/devstack").name, "devstack",
        )
        self.assertEqual(self.registry.import_metadata(a.id)["subdirectory"],
                         "examples/smoke")
        self.assertEqual([app["name"] for app in self.registry.import_metadata(a.id)["applications"]],
                         ["Web"])
        self.assertIn("tests", [task["name"] for task
                                in self.registry.import_metadata(a.id)["tasks"]])
        self.assertFalse(any(self.workspaces.base.glob(".pending-*")))

    def test_registered_projects_survive_restart_without_activation(self):
        a = self.service.import_github(URL, "smoke", "Smoke")
        service = ProjectProvisioner(
            ProjectRegistry(Database(self.registry.database.path)),
            WorkspaceStore(self.workspaces.base),
            FakeGitHub(),
        )
        self.assertEqual(service.registry.get(a.id), a)
        self.assertEqual(service.workspaces.path(a.id) / "managed" / "project.toml",
                         self.workspaces.path(a.id) / "managed" / "project.toml")
        with service.registry.database.transaction() as con:
            names = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertNotIn("runtime_state", names)

    def test_empty_project_and_imported_project_coexist(self):
        a = self.service.create_empty("blank", "Blank project")
        b = self.service.import_github(URL, "smoke", "Smoke Test")
        self.assertEqual({p.id for p in self.registry.list()}, {a.id, b.id})
        self.assertEqual(self.registry.repositories(a.id), ())
        self.assertTrue((self.workspaces.path(a.id) / "secrets").is_dir())

    def test_duplicate_slug_rejected_without_overwriting_a(self):
        a = self.service.import_github(URL, "smoke", "Smoke")
        sentinel = self.workspaces.path(a.id) / "secrets" / "private.txt"
        sentinel.write_text("preserve me")
        with self.assertRaises(ProjectConflict):
            self.service.import_github(URL, "smoke", "Another")
        self.assertEqual(sentinel.read_text(), "preserve me")
        self.assertEqual(self.registry.list(), (a,))
        self.assertFalse(list(self.workspaces.base.glob(".pending-*")))

    def test_failed_clone_is_rolled_back(self):
        class Broken:
            def clone_and_resolve(self, url, destination):
                destination.mkdir()
                (destination / "incomplete").write_text("partial clone")
                raise GitHubSourceError("network failure")
        self.service.github = Broken()
        with self.assertRaisesRegex(GitHubSourceError, "network failure"):
            self.service.import_github(URL, "broken", "Broken")
        self.assertFalse(self.registry.list())
        self.assertFalse(list(self.workspaces.base.glob(".pending-*")))

    def test_failed_manifest_is_rolled_back(self):
        class Broken:
            def clone_and_resolve(self, url, destination):
                destination.mkdir()
                manifest = destination / ".devstack"
                manifest.mkdir()
                (manifest / "project.toml").write_text("[[applications]]\nname='Bad'\n")
                return GitHubCheckout(url, "devstack", "main", "", destination)
        self.service.github = Broken()
        with self.assertRaises(ManifestError):
            self.service.import_github(URL, "invalid", "Invalid")
        self.assertFalse(self.registry.list())
        self.assertFalse(list(self.workspaces.base.glob(".pending-*")))

    def test_crash_between_publish_and_db_commit_recovers_without_deletion(self):
        original = self.registry.finalize_import
        with patch.object(self.registry, "finalize_import",
                          side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.service.import_github(URL, "pending", "Pending")
        self.assertFalse(self.registry.list())
        pending = self.registry.pending_imports()
        self.assertEqual(len(pending), 1)
        project = pending[0]
        self.assertTrue((self.workspaces.root(project.id) / "managed" / "compose.yaml").exists())
        self.assertEqual(self.service.recover_published_imports(), (project.id,))
        self.assertEqual(self.registry.list(), (project,))
        self.assertFalse(self.registry.pending_imports())

    def test_repository_files_are_not_modified_by_import(self):
        project = self.service.import_github(URL, "sample", "Sample")
        source = self.workspaces.path(project.id) / "repos" / "devstack" / "examples" / "smoke"
        self.assertEqual((source / ".devstack" / "compose.yaml").read_bytes(),
                         (SAMPLE / ".devstack" / "compose.yaml").read_bytes())
        self.assertEqual((self.workspaces.path(project.id) / "baseline" / "compose.yaml").read_bytes(),
                         (source / ".devstack" / "compose.yaml").read_bytes())


class GitHubParserTests(unittest.TestCase):
    def test_public_and_private_github_locations(self):
        public = parse_github_url(URL)
        self.assertEqual(public.repository, "devstack")
        self.assertEqual(public.tree, "main/examples/smoke")
        self.assertEqual(public.clone_url, "https://github.com/rafaelpivato/devstack.git")
        private = parse_github_url("git@github.com:org/repo.git")
        self.assertEqual(private.clone_url, "git@github.com:org/repo.git")

    def test_untrusted_urls_rejected(self):
        for url in ("https://evil.test/owner/repo", "https://github.com/owner/repo?token=x",
                    "https://github.com/owner/repo/tree/main/../secret",
                    "https://github.com/owner/repo/tree/main/a//b",
                    "https://github.com/owner/repo/tree/main/a%2fb",
                    "ssh://git@evil.test/owner/repo", "git@github.com:org/../repo"):
            with self.subTest(url=url), self.assertRaises((GitHubSourceError, ValueError)):
                parse_github_url(url)

    def test_template_discovery_is_unambiguous(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder)
            first = checkout / "a" / ".devstack"
            first.mkdir(parents=True)
            (first / "project.toml").write_text("[[applications]]\nname='API'\nservice='web'\n")
            self.assertEqual(discover_manifest(checkout, checkout, explicit=False),
                             checkout / "a")
            second = checkout / "b" / ".devstack"
            second.mkdir(parents=True)
            (second / "project.toml").write_text("[[applications]]\nname='API'\nservice='web'\n")
            with self.assertRaises(GitHubSourceError):
                discover_manifest(checkout, checkout, explicit=False)


class TemplateValidationTests(unittest.TestCase):
    def test_example_registers_web_and_tasks(self):
        template = parse_template(SAMPLE)
        self.assertEqual([(a.name, a.service, a.port) for a in template.applications],
                         [("Web", "web", 8000)])
        self.assertIn("tests", {task.name for task in template.tasks})

    def test_invalid_task_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            folder_config = source / ".devstack"
            folder_config.mkdir()
            (folder_config / "project.toml").write_text("[[applications]]\nname='Web'\nservice='web'\n")
            (folder_config / "tasks.toml").write_text(
                "[[tasks]]\nname='bad'\nservice='web'\ncommand='sh -c malicious'\n"
            )
            with self.assertRaises(ManifestError):
                parse_template(source)

    def test_manifest_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            config = source / ".devstack"
            config.mkdir()
            actual = source / "actual.toml"
            actual.write_text("[[applications]]\nname='Web'\nservice='web'\n")
            (config / "project.toml").symlink_to(actual)
            with self.assertRaises(ManifestError):
                parse_template(source)


if __name__ == "__main__":
    unittest.main()
