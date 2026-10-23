"""v0.5.2 reviewed branch changes using real local Git repositories."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from devstack.api.client import ControllerRejected
from devstack.api.http import create_app
from devstack.control.daemon import Controller, ProtocolError
from devstack.projects.repositories import (
    RepositoryGitError, RepositoryGitInvalid, RepositoryNotFound,
)
from devstack.projects.workspaces import WorkspaceStore
from devstack.storage.database import Database


def git(*args):
    result = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True,
        check=True, timeout=15,
    )
    return result.stdout.strip()


class FakeDocker:
    def __init__(self):
        self.running = []
    def owned(self):
        return list(self.running)
    def validate(self, _):
        pass
    def start(self, identifier, generation):
        self.running = [{"project": identifier, "generation": generation}]
    def verify(self, identifier, generation):
        assert self.running[0]["project"] == identifier
    def stop(self, _):
        self.running = []


class BranchSwitchingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.docker = FakeDocker()
        self.controller = Controller(
            Database(self.root / "db" / "control.sqlite3"),
            WorkspaceStore(self.root / "workspaces"), self.docker,
        )
        self.controller.coordinator.reconcile()
        self.a = self.controller.provisioner.create_empty("alpha", "Alpha").id
        self.b = self.controller.provisioner.create_empty("beta", "Beta").id
        self.backend = self._create_repo("backend", "main")
        self.frontend = self._create_repo("frontend", "develop")
        self.other_project = self._clone(self.b, self.backend, "backend")

        def transport(op, **fields):
            try:
                return self.controller.handle({"op": op, **fields})
            except Exception as exc:
                raise ControllerRejected(type(exc).__name__) from exc
        self.browser = create_app({
            "TESTING": True, "SECRET_KEY": "test",
            "SESSION_COOKIE_SECURE": False,
        }, transport=transport).test_client()
        self.browser.get("/projects/" + self.a)
        with self.browser.session_transaction() as session:
            self.csrf = session["_csrf"]

    def _create_repo(self, label, default):
        source = self.root / (label + "-source")
        origin = self.root / (label + ".git")
        git("init", "--bare", origin)
        git("init", source)
        git("-C", source, "config", "user.name", "DevStack Test")
        git("-C", source, "config", "user.email", "devstack@example.invalid")
        git("-C", source, "switch", "-c", default)
        (source / "README.md").write_text(label)
        git("-C", source, "add", ".")
        git("-C", source, "commit", "-m", "initial")
        git("-C", source, "remote", "add", "origin", origin)
        git("-C", source, "push", "-u", "origin", default)
        git("--git-dir", origin, "symbolic-ref", "HEAD", "refs/heads/" + default)
        return {"source": source, "origin": origin, "default": default,
                "name": label}

    def _clone(self, project, entry, name):
        checkout = (self.root / "workspaces" / project / "repos" / name)
        git("clone", entry["origin"], checkout)
        return self.controller.registry.add_repository(
            project, name, "repos/" + name, str(entry["origin"]),
        )

    def _publish_branch(self, entry, name):
        source = entry["source"]
        git("-C", source, "switch", "-c", name)
        (source / (name.replace("/", "_") + ".txt")).write_text("feature")
        git("-C", source, "add", ".")
        git("-C", source, "commit", "-m", "feature")
        git("-C", source, "push", "-u", "origin", name)
        git("-C", source, "switch", entry["default"])

    def _status(self, repo):
        return self.controller.repositories.status(self.a, repo.id)

    def test_project_wide_fallback_uses_verified_different_default_branches(self):
        backend = self._clone(self.a, self.backend, "backend")
        frontend = self._clone(self.a, self.frontend, "frontend")
        self._publish_branch(self.backend, "feature/checkout")
        review = self.controller.repositories.branch_preview(self.a, "feature/checkout")
        self.assertTrue(review["ready"])
        rows = {row["name"]: row for row in review["entries"]}
        self.assertEqual(rows["backend"]["target_branch"], "feature/checkout")
        self.assertFalse(rows["backend"]["fallback"])
        self.assertEqual(rows["frontend"]["target_branch"], "develop")
        self.assertTrue(rows["frontend"]["fallback"])
        self.assertEqual(rows["frontend"]["default_branch"], "develop")
        outcome = self.controller.repositories.branch_apply(
            self.a, "feature/checkout", None, review["revision"],
        )
        self.assertTrue(outcome["complete"], outcome)
        self.assertEqual(self._status(backend)["branch"], "feature/checkout")
        self.assertEqual(self._status(frontend)["branch"], "develop")
        self.assertEqual(
            self.controller.repositories.status(
                self.b, self.other_project.id,
            )["branch"], "main",
        )
        self.assertEqual(self.controller.coordinator.status()["phase"], "inactive")
        self.assertEqual(self.docker.owned(), [])

    def test_single_repository_switch_and_switch_back(self):
        backend = self._clone(self.a, self.backend, "backend")
        self._publish_branch(self.backend, "feature/mobile")
        review = self.controller.repositories.branch_preview(
            self.a, "feature/mobile", backend.id,
        )
        self.assertEqual(len(review["entries"]), 1)
        self.assertTrue(self.controller.repositories.branch_apply(
            self.a, "feature/mobile", backend.id, review["revision"],
        )["complete"])
        self.assertEqual(self._status(backend)["branch"], "feature/mobile")
        back = self.controller.repositories.branch_preview(
            self.a, "main", backend.id,
        )
        self.assertTrue(self.controller.repositories.branch_apply(
            self.a, "main", backend.id, back["revision"],
        )["complete"])
        self.assertEqual(self._status(backend)["branch"], "main")

    def test_dirty_tree_blocks_entire_project_with_no_partial_switch(self):
        backend = self._clone(self.a, self.backend, "backend")
        frontend = self._clone(self.a, self.frontend, "frontend")
        self._publish_branch(self.backend, "feature/test")
        file = self.root / "workspaces" / self.a / "repos" / "frontend" / "dirty.txt"
        file.write_text("do not discard")
        review = self.controller.repositories.branch_preview(self.a, "feature/test")
        self.assertFalse(review["ready"])
        self.assertEqual(review["entries"][-1]["action"], "blocked")
        with self.assertRaises(RepositoryGitError):
            self.controller.repositories.branch_apply(
                self.a, "feature/test", None, review["revision"],
            )
        self.assertEqual(self._status(backend)["branch"], "main")
        self.assertEqual(self._status(frontend)["branch"], "develop")
        self.assertEqual(file.read_text(), "do not discard")

    def test_stale_review_rejected_without_changing_any_repo(self):
        backend = self._clone(self.a, self.backend, "backend")
        self._publish_branch(self.backend, "feature/safe")
        review = self.controller.repositories.branch_preview(
            self.a, "feature/safe", backend.id,
        )
        # Change repo HEAD while review is open.
        checkout = self.root / "workspaces" / self.a / "repos" / "backend"
        git("-C", checkout, "config", "user.name", "DevStack Test")
        git("-C", checkout, "config", "user.email", "devstack@example.invalid")
        (checkout / "local.txt").write_text("legitimate local work")
        git("-C", checkout, "add", ".")
        git("-C", checkout, "commit", "-m", "local")
        with self.assertRaises(RepositoryGitError):
            self.controller.repositories.branch_apply(
                self.a, "feature/safe", backend.id, review["revision"],
            )
        self.assertEqual(self._status(backend)["branch"], "main")

    def test_active_runtime_and_other_project_repository_rejected(self):
        backend = self._clone(self.a, self.backend, "backend")
        with self.assertRaises(RepositoryNotFound):
            self.controller.repositories.branch_preview(
                self.a, "main", self.other_project.id,
            )
        self.controller.coordinator.activate(self.b)
        with self.assertRaises(RepositoryGitError):
            self.controller.repositories.branch_preview(
                self.a, "main", backend.id,
            )
        self.controller.coordinator.deactivate()

    def test_branch_input_validation_and_no_arbitrary_controller_operation(self):
        backend = self._clone(self.a, self.backend, "backend")
        for branch in ("-bad", "a..b", "a//b", "../main", "refs/heads/main",
                       "a.lock", "a/.lock", "bad name", "a@{2}", ""):
            # refs/heads/main is technically a valid string but it must not
            # become a valid upstream branch without an actual remote ref.
            if branch == "refs/heads/main":
                continue
            with self.subTest(branch=branch), self.assertRaises(
                RepositoryGitInvalid
            ):
                self.controller.repositories.branch_preview(
                    self.a, branch, backend.id,
                )
        with self.assertRaises(ProtocolError):
            self.controller.handle({
                "op": "branch_apply", "project_id": self.a,
                "branch": "main", "repository_id": backend.id,
                "revision": "0"*64, "argv": ["git", "reset", "--hard"],
            })

    def test_mobile_preview_requires_confirmation_and_csrf(self):
        backend = self._clone(self.a, self.backend, "backend")
        self._publish_branch(self.backend, "feature/ui")
        url = "/projects/" + self.a + "/repositories/branch"
        preview = self.browser.get(url, query_string={
            "branch": "feature/ui", "repository_id": backend.id,
        })
        self.assertEqual(preview.status_code, 200, preview.data[:200])
        self.assertIn(b"devstack-csrf", preview.data)
        self.assertEqual(self._status(backend)["branch"], "main")
        reviewed = self.controller.repositories.branch_preview(
            self.a, "feature/ui", backend.id,
        )
        apply = "/api/v2/projects/" + self.a + "/git/branch-apply"
        payload = {
            "branch": "feature/ui", "repository_id": backend.id,
            "revision": reviewed["revision"],
        }
        self.assertEqual(self.browser.post(apply, json=payload).status_code, 403)
        response = self.browser.post(
            apply, json=payload, headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["complete"])
        self.assertEqual(self._status(backend)["branch"], "feature/ui")
        self.assertEqual(self.browser.post(
            apply, json={**payload, "argv": ["reset"]},
            headers={"X-CSRF-Token": self.csrf},
        ).status_code, 400)


if __name__ == "__main__":
    unittest.main()
