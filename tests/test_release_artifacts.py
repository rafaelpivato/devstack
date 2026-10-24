"""Tests for the v0.6.5 source-to-wheel transition and fail-closed validation."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from scripts import updater


def wheel_bytes():
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("devstack-0.6.6.dist-info/METADATA",
                         "Name: devstack\nVersion: 0.6.6\n")
        archive.writestr("devstack/templates/app.html", "<html>React</html>")
        archive.writestr("devstack/static/v06/manifest.json",
                         json.dumps({"src/main.tsx": {
                             "file": "assets/main-123.js",
                             "css": ["assets/style-123.css"]}}))
        archive.writestr("devstack/static/v06/assets/main-123.js", "js")
        archive.writestr("devstack/static/v06/assets/style-123.css", "css")
    return stream.getvalue()


class ArtifactReleaseTests(unittest.TestCase):
    def test_legacy_to_artifact_cutover_is_explicit(self):
        self.assertLess(updater.release_version("v0.6.5"), updater.WHEEL_FROM)
        self.assertEqual(updater.release_version("v0.6.6"), updater.WHEEL_FROM)

    def _metadata(self, data):
        name = "devstack-0.6.6-py3-none-any.whl"
        checksum = (hashlib.sha256(data).hexdigest() + "  " + name + "\n").encode()
        prefix = "https://github.com/rafaelpivato/devstack/releases/download/v0.6.6/"
        def asset(n, body):
            return {"name": n, "state": "uploaded", "size": len(body),
                    "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                    "browser_download_url": prefix + n}
        release = {"tag_name": "v0.6.6", "draft": False, "prerelease": False,
                   "published_at": "2026-10-24T00:00:00Z", "immutable": True,
                   "assets": [asset(name, data), asset("SHA256SUMS", checksum)]}
        return release, checksum

    def test_accepts_matching_published_release(self):
        wheel = wheel_bytes()
        release, checksum = self._metadata(wheel)
        with patch.object(updater, "download", side_effect=[
            json.dumps(release).encode(), checksum, wheel
        ]):
            filename, result = updater.verified_wheel("rafaelpivato/devstack", "v0.6.6")
        self.assertEqual(filename, "devstack-0.6.6-py3-none-any.whl")
        self.assertEqual(result, wheel)

    def test_rejects_draft_and_missing_or_extra_artifacts(self):
        wheel = wheel_bytes()
        release, checksum = self._metadata(wheel)
        for modification in ("draft", "missing", "extra"):
            with self.subTest(modification=modification):
                candidate = dict(release)
                if modification == "draft":
                    candidate["draft"] = True
                elif modification == "missing":
                    candidate["assets"] = release["assets"][:1]
                else:
                    candidate["assets"] = release["assets"] + [dict(release["assets"][0])]
                with patch.object(updater, "download", return_value=json.dumps(candidate).encode()):
                    with self.assertRaises(RuntimeError):
                        updater.verified_wheel("rafaelpivato/devstack", "v0.6.6")

    def test_rejects_bad_checksum_and_version_mismatch(self):
        wheel = wheel_bytes()
        release, checksum = self._metadata(wheel)
        wrong = b"0" * 64 + checksum[64:]
        with patch.object(updater, "download", side_effect=[
            json.dumps(release).encode(), wrong
        ]):
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                updater.verified_wheel("rafaelpivato/devstack", "v0.6.6")
        with patch.object(updater, "download", side_effect=[
            json.dumps(release).encode(), checksum, wheel + b"corrupt"
        ]):
            with self.assertRaisesRegex(RuntimeError, "length mismatch"):
                updater.verified_wheel("rafaelpivato/devstack", "v0.6.6")

    def test_missing_asset_does_not_switch_running_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "app"
            state = Path(folder) / "state"
            previous = root / "releases/v0.6.5"
            previous.mkdir(parents=True)
            (previous / ".ready").write_text("v0.6.5\n")
            updater.point_at(root / "current", previous)
            with patch("scripts.updater.healthy", return_value=True), patch(
                "scripts.updater.verified_wheel", side_effect=RuntimeError("asset missing")
            ):
                with self.assertRaisesRegex(RuntimeError, "asset missing"):
                    updater.activate("v0.6.6", "rafaelpivato/devstack", root, state)
            self.assertEqual(updater.current_release(root / "current"), previous)
            self.assertFalse((root / "releases/v0.6.6").exists())

    def test_unsafe_redirect_rejected(self):
        import urllib.request
        request = urllib.request.Request("https://github.com")
        for url in ("http://github.com/evil", "https://github.com.attacker.invalid/file"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                    updater.SafeRedirect().redirect_request(
                        request, None, 302, "Moved", {}, url)


if __name__ == "__main__":
    unittest.main()
