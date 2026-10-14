import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import updater


class ValidationTests(unittest.TestCase):
    def test_version_validation(self):
        self.assertEqual(updater.checked_tag("v1.2.3"), "v1.2.3")
        for tag in ["main", "../v1.2.3", "v1.0.0-rc1", "--force"]:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                updater.checked_tag(tag)

    def test_repo_validation(self):
        self.assertEqual(updater.checked_repo("rafaelpivato/devstack"),
                         "rafaelpivato/devstack")
        for repo in ["../evil", "foo/bar/baz", "foo/..", "-invalid/../foo"]:
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                updater.checked_repo(repo)


class DeploymentTests(unittest.TestCase):
    def test_atomic_pointer_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            previous = root / "previous"
            next_version = root / "next"
            previous.mkdir()
            next_version.mkdir()
            current = root / "current"
            updater.point_at(current, previous)
            self.assertEqual(updater.current_release(current), previous)
            updater.point_at(current, next_version)
            self.assertEqual(updater.current_release(current), next_version)

    @patch("scripts.updater.execute")
    @patch("scripts.updater.healthy", return_value=True)
    def test_update_success(self, healthy, execute):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "app"
            state = Path(d) / "state"
            release = root / "releases/v1.0.0"
            release.mkdir(parents=True)
            (release / ".ready").write_text("v1.0.0")
            status = updater.activate("v1.0.0", "rafaelpivato/devstack", root, state)
            self.assertIn("Activated", status)
            self.assertEqual(updater.current_release(root / "current"), release)
            execute.assert_called_once_with("systemctl", "restart", "devstack.service", timeout=30)

    @patch("scripts.updater.subprocess.run")
    @patch("scripts.updater.execute")
    @patch("scripts.updater.healthy", return_value=False)
    def test_failed_update_restores_previous(self, healthy, execute, process):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "app"
            state = Path(d) / "state"
            old = root / "releases/v1.0.0"
            new = root / "releases/v1.0.1"
            old.mkdir(parents=True)
            new.mkdir()
            (new / ".ready").write_text("v1.0.1")
            updater.point_at(root / "current", old)
            with self.assertRaisesRegex(RuntimeError, "restored previous"):
                updater.activate("v1.0.1", "rafaelpivato/devstack", root, state)
            self.assertEqual(updater.current_release(root / "current"), old)
            self.assertTrue((state / "failed-v1.0.1").exists())
            process.assert_called_once()
            self.assertEqual(process.call_args.args[0][:2], ["systemctl", "restart"])

    @patch("scripts.updater.execute")
    def test_failed_tag_not_retried(self, execute):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "app"
            state = Path(d) / "state"
            state.mkdir()
            (state / "failed-v1.0.1").write_text("bad release")
            status = updater.activate("v1.0.1", "rafaelpivato/devstack", root, state)
            self.assertIn("Skipping", status)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
