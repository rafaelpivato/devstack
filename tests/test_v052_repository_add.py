"""Real filesystem/SQLite/API tests of project-owned secondary Git repositories."""
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from devstack.control.daemon import Controller, ProtocolError
from devstack.control.repository_add import (
    RepositoryAddConflict, RepositoryAddNotFound,
)
from devstack.api.client import ControllerRejected
from devstack.api.http import create_app
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database


def git(*args):
    return subprocess.run(
        ["git", *map(str, args)], check=True, text=True,
        capture_output=True, timeout=15,
    ).stdout.strip()


class FakeClone:
    def __init__(self):
        self.clones = []
        self.fail = False

    def clone_repository(self, url, destination, branch=""):
        self.clones.append((url, destination, branch))
        destination.mkdir()
        if self.fail:
            raise RuntimeError("simulated network error")
        git("init", destination)
        git("-C", destination, "config", "user.name", "DevStack Test")
        git("-C", destination, "config", "user.email", "test@example.invalid")
        (destination / "readme.txt").write_text("isolated checkout")
        git("-C", destination, "add", ".")
        git("-C", destination, "commit", "-m", "initial")
        git("-C", destination, "remote", "add", "origin", url)
        return git("-C", destination, "rev-parse", "HEAD")


class Docker:
    def __init__(self):
        self.running = []
    def owned(self):
        return list(self.running)
    def validate(self, _):
        return None
    def start(self, identifier, generation):
        self.running = [{"project": identifier, "generation": generation}]
    def verify(self, identifier, generation):
        assert self.running[0]["project"] == identifier
    def stop(self, identifier):
        self.running.clear()


class MultiRepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.docker = Docker()
        self.controller = Controller(
            Database(self.root / "db" / "control.sqlite3"),
            WorkspaceStore(self.root / "projects"), self.docker,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "A")
        self.b = self.controller.provisioner.create_empty("beta", "B")
        self.fake = FakeClone()
        self.controller.repository_add.github = self.fake
        self.url = "https://github.com/org/backend.git"

    def submit(self, project, name="backend", key=None, url=None, branch=""):
        return self.controller.repository_add.submit(
            project, url or self.url, name, branch, key or uuid4().hex,
        )

    def wait(self, project, operation):
        for _ in range(100):
            result = self.controller.repository_add.get(project, operation["id"])
            if result["status"] in ("succeeded", "failed", "recovery"):
                return result
            time.sleep(.02)
        self.fail("Timed out waiting for Git clone")

    def test_git_adapter_clones_real_repository_without_devstack_manifest(self):
        from devstack.adapters.github import GitHubSource, GitHubLocation
        original = self.root / "original"
        origin = self.root / "origin.git"
        git("init", original)
        git("-C", original, "config", "user.name", "DevStack Test")
        git("-C", original, "config", "user.email", "test@example.invalid")
        (original / "readme.txt").write_text("ordinary source")
        git("-C", original, "add", ".")
        git("-C", original, "commit", "-m", "initial")
        git("-C", original, "branch", "-M", "main")
        git("init", "--bare", origin)
        git("-C", original, "remote", "add", "origin", origin)
        git("-C", original, "push", "-u", "origin", "main")
        git("--git-dir", origin, "symbolic-ref", "HEAD", "refs/heads/main")
        target = self.root / "ordinary"
        with patch("devstack.adapters.github.parse_github_url",
                   return_value=GitHubLocation(str(origin), "backend", None)):
            head = GitHubSource().clone_repository(
                "https://github.com/org/backend", target, "main"
            )
        self.assertEqual(git("-C", target, "rev-parse", "HEAD"), head)
        self.assertEqual((target / "readme.txt").read_text(), "ordinary source")
        self.assertFalse((target / ".devstack").exists())

    def test_multiple_repos_and_projects_persist_without_activation(self):
        one = self.wait(self.a.id, self.submit(self.a.id, name="backend"))
        two = self.wait(self.a.id, self.submit(self.a.id, name="frontend"))
        three = self.wait(self.b.id, self.submit(self.b.id, name="backend"))
        self.assertEqual({x["status"] for x in (one,two,three)}, {"succeeded"})
        self.assertEqual(len(self.controller.registry.repositories(self.a.id)), 2)
        self.assertEqual(len(self.controller.registry.repositories(self.b.id)), 1)
        self.assertNotEqual(one["repo_id"], three["repo_id"])
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")
        self.assertFalse(self.docker.running)
        self.assertNotEqual(
            self.controller.repositories.status(self.a.id, one["repo_id"])["id"],
            three["repo_id"],
        )
        restarted = Controller(
            Database(self.root / "db" / "control.sqlite3"),
            WorkspaceStore(self.root / "projects"), self.docker,
        )
        self.assertEqual(len(restarted.registry.repositories(self.a.id)), 2)
        self.assertEqual(restarted.coordinator.status()["phase"], "inactive")

    def test_idempotent_repeat_and_collision_do_not_overwrite_checkout(self):
        key = uuid4().hex
        first = self.submit(self.a.id, key=key)
        again = self.submit(self.a.id, key=key)
        self.assertEqual(first["id"], again["id"])
        completed = self.wait(self.a.id, first)
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(len(self.fake.clones), 1)
        with self.assertRaises(RepositoryAddConflict):
            self.submit(self.a.id, name="backend", key=key,
                        url="https://github.com/org/other.git")
        with self.assertRaises(RepositoryAddConflict):
            self.submit(self.a.id, name="backend")
        self.assertEqual(len(self.controller.registry.repositories(self.a.id)), 1)
        self.assertEqual(
            (self.root / "projects" / self.a.id / "repos" / "backend"
             / "readme.txt").read_text(), "isolated checkout",
        )

    def test_private_repository_ssh_url_is_normalized_and_branch_is_preserved(self):
        completed = self.wait(self.a.id, self.submit(
            self.a.id, url="git@github.com:org/private.git",
            name="private", branch="dev",
        ))
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(self.fake.clones[0][0], "git@github.com:org/private.git")
        self.assertEqual(self.fake.clones[0][2], "dev")

    def test_fail_closed_on_active_runtime_and_cross_project_id(self):
        self.controller.coordinator.activate(self.a.id)
        with self.assertRaises(RepositoryAddConflict):
            self.submit(self.a.id)
        with self.assertRaises(RepositoryAddConflict):
            self.submit(self.b.id)
        self.controller.coordinator.deactivate()
        result = self.wait(self.a.id, self.submit(self.a.id))
        with self.assertRaises(RepositoryAddNotFound):
            self.controller.repository_add.get(self.b.id, result["id"])
        with self.assertRaises(ProtocolError):
            self.controller.handle({
                "op": "repository_add_submit", "project_id": self.a.id,
                "url": self.url, "name": "api", "branch": "",
                "request_key": uuid4().hex, "command": "curl",
            })

    def test_invalid_urls_branches_and_tree_paths_never_spawn_git(self):
        for url,branch in (
            ("https://evil.example/org/backend", ""),
            ("https://github.com/org/backend/tree/main/path", ""),
            ("https://github.com/org/backend", "../main"),
            ("https://github.com/org/backend", "-C"),
        ):
            with self.subTest(url=url,branch=branch), self.assertRaises(Exception):
                self.submit(self.a.id, url=url, branch=branch)
        self.assertFalse(self.fake.clones)

    def test_failed_clone_preserves_staging_and_never_registers(self):
        self.fake.fail = True
        failed = self.wait(self.a.id, self.submit(self.a.id))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.controller.registry.repositories(self.a.id), ())
        staged = self.root / "projects" / self.a.id / "repos" / (
            ".repo-add-" + failed["id"]
        )
        self.assertTrue(staged.is_dir())
        self.assertEqual(self.controller.repository_add.reconcile(), ())
        self.assertTrue(staged.is_dir())

    def test_crash_after_rename_recovers_published_checkout(self):
        from devstack.control import repository_add
        original = repository_add.os.rename

        def crashed(source, destination):
            original(source, destination)
            raise OSError("simulated crash after publish")

        with patch.object(repository_add.os, "rename", side_effect=crashed):
            operation = self.submit(self.a.id)
            pending = self.wait(self.a.id, operation)
        self.assertEqual(pending["status"], "recovery")
        self.assertEqual(self.controller.registry.repositories(self.a.id), ())
        self.assertTrue((self.root/"projects"/self.a.id/"repos"/"backend").is_dir())
        self.assertEqual(self.controller.repository_add.reconcile(), (pending["id"],))
        self.assertEqual(len(self.controller.registry.repositories(self.a.id)), 1)
        self.assertEqual(
            self.controller.repository_add.get(self.a.id, pending["id"])["status"],
            "succeeded",
        )

    def test_api_csrf_bounded_args_and_ssh_public_key_only(self):
        def transport(op, **kwargs):
            try:
                return self.controller.handle({"op":op, **kwargs})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc
        browser = create_app({
            "TESTING": True, "SECRET_KEY": "test-only",
            "SESSION_COOKIE_SECURE": False,
        }, transport=transport).test_client()
        with patch("devstack.control.daemon.public_key", return_value=None):
            self.assertEqual(browser.get("/").status_code, 200)
        with browser.session_transaction() as session:
            token = session["_csrf"]
        path = "/api/v2/projects/" + self.a.id + "/repositories"
        headers = {"X-CSRF-Token": token, "Idempotency-Key": uuid4().hex}
        payload = {"name":"backend", "url":self.url, "branch":""}
        self.assertEqual(browser.post(path,json=payload,
                         headers={"Idempotency-Key":headers["Idempotency-Key"]}).status_code,403)
        self.assertEqual(browser.post(path,json={"name":"backend"},
                         headers=headers).status_code,400)
        result = browser.post(path,json={"name":"backend", "url":self.url},
                              headers=headers)
        self.assertEqual(result.status_code, 202, result.get_json())
        job = self.wait(self.a.id, result.get_json()["import"])
        self.assertEqual(job["status"], "succeeded")
        status_url = "/api/v2/projects/" + self.a.id + "/repository-adds/" + job["id"]
        self.assertEqual(browser.get(status_url).status_code, 200)
        self.assertEqual(browser.get(
            "/api/v2/projects/" + self.b.id + "/repository-adds/" + job["id"]
        ).status_code, 404)
        with patch("devstack.control.daemon.public_key", return_value="ssh-ed25519 TEST"), \
             patch("devstack.control.daemon.generate_key", return_value="ssh-ed25519 TEST"), \
             patch("devstack.control.daemon.test_connection", return_value="account"):
            self.assertEqual(browser.get("/api/v2/github/ssh").get_json(),
                             {"public_key":"ssh-ed25519 TEST"})
            self.assertEqual(browser.post("/api/v2/github/ssh/generate",
                             headers={"X-CSRF-Token":token}).get_json(),
                             {"public_key":"ssh-ed25519 TEST"})
            self.assertEqual(browser.post("/api/v2/github/ssh/test",
                             headers={"X-CSRF-Token":token}).get_json(),
                             {"authenticated_user":"account"})


if __name__ == "__main__":
    unittest.main()
