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
from devstack.projects.service import ProjectProvisioner, ProjectProvisionError
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
        self.assertEqual(self.service.source_directory(a.id),
                         self.workspaces.path(a.id) / "repos" / "devstack"
                         / "examples" / "smoke" / ".devstack")
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
        self.assertIn("runtime_state", names)
        with service.registry.database.transaction() as con:
            state = con.execute("SELECT phase,project_id FROM runtime_state").fetchone()
        self.assertEqual(tuple(state), ("inactive", None))

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

    def test_interrupted_import_refuses_corrupted_published_manifest(self):
        with patch.object(self.registry, "finalize_import",
                          side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.service.import_github(URL, "unsafe", "Unsafe")
        pending = self.registry.pending_imports()
        self.assertEqual(len(pending), 1)
        broken_file = self.workspaces.root(pending[0].id) / "managed" / "project.toml"
        broken_file.unlink()
        with self.assertRaisesRegex(ProjectProvisionError, "Missing managed import baseline"):
            self.service.recover_published_imports()
        self.assertFalse(self.registry.list())
        self.assertEqual(self.registry.pending_imports(), pending)

    def test_repository_files_are_not_modified_by_import(self):
        project = self.service.import_github(URL, "sample", "Sample")
        source = self.workspaces.path(project.id) / "repos" / "devstack" / "examples" / "smoke"
        self.assertEqual((source / ".devstack" / "compose.yaml").read_bytes(),
                         (SAMPLE / ".devstack" / "compose.yaml").read_bytes())
        self.assertEqual((self.workspaces.path(project.id) / "baseline" / "compose.yaml").read_bytes(),
                         (source / ".devstack" / "compose.yaml").read_bytes())



class DeclarativeRepositoryImportTests(unittest.TestCase):
    """GitHub source templates declare independent, validated repos."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.registry = ProjectRegistry(Database(self.base / "db" / "control.sqlite3"))
        self.workspaces = WorkspaceStore(self.base / "workspaces")

    def _adapter(self, manifest, *, fail_name=None):
        class MultiGitHub(FakeGitHub):
            def clone_and_resolve(self, url, destination):
                checkout = super().clone_and_resolve(url, destination)
                (checkout.manifest_dir / ".devstack" / "repositories.toml").write_text(
                    manifest, encoding="utf-8",
                )
                return checkout

            def clone_repository(self, url, destination, branch=""):
                if destination.name == fail_name:
                    raise GitHubSourceError("Simulated inaccessible additional repository")
                destination.mkdir()
                (destination / ".git").mkdir()
                return "a" * 40

        return MultiGitHub()

    def _import(self, manifest, *, fail_name=None):
        service = ProjectProvisioner(
            self.registry, self.workspaces, self._adapter(manifest, fail_name=fail_name),
        )
        return service.import_github(URL, "outreach", "TrustedOutreach")

    def test_one_import_registers_source_and_three_independent_repositories(self):
        manifest = """
[[repositories]]
name = 'beacon-django'
url = 'git@github.com:example/beacon-django.git'
branch = 'main'

[[repositories]]
name = 'beacon-react'
url = 'https://github.com/example/beacon-react'

[[repositories]]
name = 'conduit-elixir'
url = 'git@github.com:example/conduit-elixir.git'
"""
        entry = self._import(manifest)
        repos = self.registry.repositories(entry.id)
        self.assertEqual(
            {r.name for r in repos},
            {"devstack", "beacon-django", "beacon-react", "conduit-elixir"},
        )
        for name in ("beacon-django", "beacon-react", "conduit-elixir"):
            self.assertTrue(self.workspaces.repository(entry.id, "repos/" + name).is_dir())
        self.assertEqual(
            {r.name: r.clone_url for r in repos}["beacon-react"],
            "https://github.com/example/beacon-react.git",
        )
        self.assertEqual(self.registry.import_metadata(entry.id)["subdirectory"], "examples/smoke")

    def test_failed_extra_clone_keeps_project_invisible(self):
        manifest = """
[[repositories]]
name = 'beacon-django'
url = 'git@github.com:example/beacon-django.git'

[[repositories]]
name = 'conduit-elixir'
url = 'git@github.com:example/conduit-elixir.git'
"""
        with self.assertRaises(GitHubSourceError):
            self._import(manifest, fail_name="conduit-elixir")
        self.assertEqual(self.registry.list(), ())
        self.assertEqual(self.registry.pending_imports(), ())
        self.assertEqual(list(self.workspaces.base.glob(".pending-*")), [])

    def test_rejects_repeating_the_umbrella_source(self):
        manifest = """
[[repositories]]
name = 'other-alias'
url = 'https://github.com/rafaelpivato/devstack'
"""
        with self.assertRaises(ManifestError):
            self._import(manifest)
        self.assertEqual(self.registry.list(), ())

    def test_rejects_duplicate_names_urls_invalid_branches_and_external_urls(self):
        manifests = (
            "[[repositories]]\nname='a'\nurl='https://github.com/x/one'\n"
            "[[repositories]]\nname='a'\nurl='https://github.com/x/two'\n",
            "[[repositories]]\nname='a'\nurl='https://github.com/x/one'\n"
            "[[repositories]]\nname='b'\nurl='https://github.com/x/one.git'\n",
            "[[repositories]]\nname='a'\nurl='https://example.com/x/one'\n",
            "[[repositories]]\nname='a'\nurl='https://github.com/x/one/tree/main'\n",
            "[[repositories]]\nname='a'\nurl='https://github.com/x/one'\n"
            "branch='--upload-pack=bad'\n",
        )
        for manifest in manifests:
            with self.subTest(manifest=manifest), self.assertRaises(ManifestError):
                self._import(manifest)
            self.assertFalse(self.registry.list())

    def test_recovery_checks_every_imported_checkout_before_finalization(self):
        manifest = """
[[repositories]]
name = 'beacon-django'
url = 'git@github.com:example/beacon-django.git'
"""
        service = ProjectProvisioner(
            self.registry, self.workspaces, self._adapter(manifest),
        )
        with patch.object(self.registry, "finalize_import",
                          side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                service.import_github(URL, "outreach", "TrustedOutreach")
        pending = self.registry.pending_imports()
        self.assertEqual(len(pending), 1)
        extra = self.workspaces.root(pending[0].id) / "repos" / "beacon-django"
        shutil.rmtree(extra)
        with self.assertRaisesRegex(ProjectProvisionError, "Missing imported repository checkout"):
            service.recover_published_imports()
        self.assertEqual(self.registry.list(), ())


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
