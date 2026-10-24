#!/usr/bin/env python3
"""Fail-closed verification for CI-built Python wheel and frontend manifest."""
import base64
import csv
import hashlib
import io
import json
import re
import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile

MAX_ENTRIES = 5000
MAX_UNPACKED_BYTES = 128 * 1024 * 1024


def validate(folder: Path, root: Path = Path(".")) -> Path:
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    frontend = json.loads((root / "frontend/package.json").read_text())
    lock = json.loads((root / "frontend/package-lock.json").read_text())
    if frontend["version"] != version or lock["version"] != version:
        raise ValueError("Python/frontend/lockfile versions disagree")
    wheels = sorted(folder.glob("*.whl"))
    name = f"devstack-{version}-py3-none-any.whl"
    if len(wheels) != 1 or wheels[0].name != name:
        raise ValueError(f"Exactly one versioned release wheel required: {name}")
    with ZipFile(wheels[0]) as archive:
        members = archive.infolist()
        if len(members) > MAX_ENTRIES or sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
            raise ValueError("Wheel archive exceeds size or member limits")
        paths = [m.filename for m in members]
        if len(set(paths)) != len(paths):
            raise ValueError("Duplicate paths in wheel")
        if any(n.startswith("/") or ".." in n.split("/") for n in paths):
            raise ValueError("Unsafe wheel archive path")
        names = set(paths)
        prefix = "devstack/static/v06/"
        meta_dir = f"devstack-{version}.dist-info/"
        for required in (meta_dir + "METADATA", meta_dir + "WHEEL", meta_dir + "RECORD",
                         "devstack/_standalone_updater.py",
                         "devstack/templates/app.html", prefix + "manifest.json"):
            if required not in names:
                raise ValueError(f"Missing distribution member: {required}")
        if b"Independent, conservative release updater" not in archive.read("devstack/_standalone_updater.py")[:512]:
            raise ValueError("Missing trusted standalone updater identity")
        if archive.read("devstack/_standalone_updater.py") != (root / "scripts/updater.py").read_bytes():
            raise ValueError("Staged standalone updater differs from source")
        metadata = archive.read(meta_dir + "METADATA").decode("utf-8").splitlines()
        if "Name: devstack" not in metadata or f"Version: {version}" not in metadata:
            raise ValueError("Wheel metadata version differs from tag")
        manifest = json.loads(archive.read(prefix + "manifest.json"))
        main = manifest["src/main.tsx"]
        if not main.get("isEntry") or not main["file"].endswith(".js"):
            raise ValueError("Missing JavaScript entry")
        assets = set()
        for entry in manifest.values():
            if "file" in entry:
                assets.add(entry["file"])
            assets.update(entry.get("css", ()))
            assets.update(entry.get("assets", ()))
        if not any(x.endswith(".js") for x in assets) or not any(
                x.endswith(".css") for x in assets):
            raise ValueError("Missing JavaScript or CSS assets")
        for path in assets:
            if not re.fullmatch(r"assets/[A-Za-z0-9._/-]+", path) or ".." in path.split("/"):
                raise ValueError("Invalid manifest asset path")
            if prefix + path not in names:
                raise ValueError(f"Missing bundled file: {path}")
        if any(p.startswith(("devstack/templates/v2_", "devstack/static/js/v2_"))
               for p in names):
            raise ValueError("Unexpected legacy UI content")
        # Validate wheel RECORD checksums against all archived bytes.
        records = {}
        for path, digest, size in csv.reader(
                io.StringIO(archive.read(meta_dir + "RECORD").decode("utf-8"))):
            if path in records:
                raise ValueError("Duplicate wheel RECORD entries")
            records[path] = (digest, size)
        if names != set(records):
            raise ValueError("Wheel RECORD does not describe every archive member")
        for path in names:
            if path == meta_dir + "RECORD":
                continue
            digest, size = records[path]
            content = archive.read(path)
            actual = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            if digest != "sha256=" + actual or size != str(len(content)):
                raise ValueError(f"Wheel RECORD integrity mismatch: {path}")
        if archive.testzip() is not None:
            raise ValueError("Wheel contains corrupt ZIP entries")
    return wheels[0]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_release_wheel.py DIST_DIRECTORY")
    print(validate(Path(sys.argv[1]), Path(__file__).resolve().parents[1]))
