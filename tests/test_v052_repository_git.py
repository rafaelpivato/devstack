"""ADR INV-01/03/05/09: real Git repositories through UUID-scoped v2 IPC."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from uuid import uuid4

from devstack.api.client import ControllerRejected
from devstack.api.http import create_app
from devstack.control.daemon import Controller, ProtocolError
from devstack.projects.repositories import (
    RepositoryGitError, RepositoryNotFound, RepositoryGitInvalid,
)
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database


def git(*args):
    completed = subprocess.run(["git", *map(str, args)], text=True,
                               capture_output=True, check=True, timeout=12)
    return completed.stdout.strip()


class Docker:
    def __init__(self):
        self.running = []
    def owned(self):
        return list(self.running)
    def validate(self, _project):
        pass
    def start(self, project, generation):
        self.running = [{"project": project, "generation": generation}]
    def verify(self, project, generation):
        assert self.running and self.running[0]["project"] == project
    def stop(self, _project):
        self.running = []


class GitRepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        origin = self.root / "origin.git"
        self.origin = origin
        git("init", "--bare", origin)
        source = self.root / "source"
        git("init", source)
        git("-C", source, "config", "user.name", "DevStack Test")
        git("-C", source, "config", "user.email", "devstack@example.invalid")
        git("-C", source, "checkout", "-b", "main")
        (source / "readme.txt").write_text("initial")
        git("-C", source, "add", ".")
        git("-C", source, "commit", "-m", "initial")
        git("-C", source, "remote", "add", "origin", origin)
        git("-C", source, "push", "-u", "origin", "main")
        git("--git-dir", origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.source = source
        self.docker = Docker()
        self.controller = Controller(
            Database(self.root / "db" / "control.sqlite3"),
            WorkspaceStore(self.root / "projects"), self.docker,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "A")
        self.b = self.controller.provisioner.create_empty("beta", "B")
        self.a_repo = self._clone(self.a.id)
        self.b_repo = self._clone(self.b.id)

    def _clone(self, identifier):
        checkout = self.root / "projects" / identifier / "repos" / "code"
        git("clone", self.origin, checkout)
        return self.controller.registry.add_repository(
            identifier, "code", "repos/code", str(self.origin),
        )

    def _advance(self):
        (self.source / "readme.txt").write_text("new version")
        git("-C", self.source, "commit", "-am", "next")
        git("-C", self.source, "push", "origin", "main")

    def test_two_projects_update_independently_without_activation(self):
        a, b = self.a.id, self.b.id
        first = self.controller.repositories.status(a, self.a_repo.id)
        self._advance()
        updated = self.controller.repositories.update(a, self.a_repo.id, first["head"])
        self.assertNotEqual(updated["head"], first["head"])
        self.assertEqual(self.controller.repositories.status(b, self.b_repo.id)["head"],
                         first["head"])
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")
        self.assertEqual(self.docker.running, [])

    def test_dirty_stale_and_active_updates_refused(self):
        project = self.a.id
        head = self.controller.repositories.status(project, self.a_repo.id)["head"]
        with self.assertRaises(RepositoryGitInvalid):
            self.controller.repositories.update(project, self.a_repo.id, "wrong")
        with self.assertRaises(RepositoryGitError):
            self.controller.repositories.update(project, self.a_repo.id, "0" * 40)
        self._advance()
        (self.root / "projects" / project / "repos" / "code" / "untracked.txt").write_text("local")
        with self.assertRaises(RepositoryGitError):
            self.controller.repositories.update(project, self.a_repo.id, head)
        (self.root / "projects" / project / "repos" / "code" / "untracked.txt").unlink()
        self.controller.coordinator.activate(self.b.id)
        with self.assertRaises(RepositoryGitError):
            self.controller.repositories.update(project, self.a_repo.id, head)
        self.assertEqual(self.controller.coordinator.status()["project_id"], self.b.id)
        self.controller.coordinator.deactivate()

    def test_unknown_and_cross_project_repo_ids_fail_closed(self):
        with self.assertRaises(RepositoryNotFound):
            self.controller.repositories.status(self.a.id, self.b_repo.id)
        with self.assertRaises(RepositoryNotFound):
            self.controller.repositories.status(self.a.id, str(uuid4()))
        with self.assertRaises(ProtocolError):
            self.controller.handle({"op": "repository_update", "project_id": self.a.id,
                                    "repository_id": self.a_repo.id,
                                    "expected_head": "0"*40, "command": "rm"})

    def test_v2_api_and_browser_do_not_expose_git_commands(self):
        def transport(op, **args):
            try:
                return self.controller.handle({"op": op, **args})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc
        app = create_app({"TESTING": True, "SECRET_KEY": "test",
                          "SESSION_COOKIE_SECURE": False}, transport=transport)
        browser = app.test_client()
        project = self.a.id
        detail = browser.get("/projects/" + project)
        self.assertEqual(detail.status_code, 200)
        with browser.session_transaction() as session:
            csrf = session["_csrf"]
        status_url = f"/api/v2/projects/{project}/repositories/{self.a_repo.id}"
        status = browser.get(status_url)
        self.assertEqual(status.status_code, 200)
        head = status.get_json()["head"]
        self.assertEqual(browser.get(
            f"/projects/{project}/repositories/{self.a_repo.id}").status_code, 200)
        self.assertEqual(browser.get(
            f"/api/v2/projects/{project}/repositories/{self.b_repo.id}").status_code, 404)
        self.assertEqual(browser.post(status_url + "/update", json={"expected_head": head}
                         ).status_code, 403)
        result = browser.post(
            status_url + "/update", json={"expected_head": head},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")


if __name__ == "__main__":
    unittest.main()
