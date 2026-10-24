#!/usr/bin/env python3
"""One-time v0.6.5 host bridge from immutable-only to verified mutable wheels.

Run only from the exact published v0.6.6 tagged Git checkout. This does NOT
activate a release or restart services; it atomically replaces the standalone,
root-owned updater with the version embedded in the verified published wheel.
"""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from zipfile import ZipFile
import io

ROOT = Path(__file__).resolve().parents[1]
TAG = "v0.6.6"
REPO = "rafaelpivato/devstack"
HOST_ROOT = Path("/opt/devstack")
DESTINATION = Path("/usr/local/lib/devstack/updater.py")
EMBEDDED = "devstack/_standalone_updater.py"
IDENTITY = b"Independent, conservative release updater"


def assert_published_checkout(root: Path = ROOT) -> None:
    """Do not accept arbitrary source branches, untracked replacement code."""
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, check=True, timeout=15)
        return result.stdout.strip()
    if git("describe", "--tags", "--exact-match", "HEAD") != TAG:
        raise RuntimeError("Must run from the published v0.6.6 Git tag")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Tracked checkout has uncommitted modifications")
    import tomllib
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    if metadata["project"]["version"] != TAG[1:]:
        raise RuntimeError("Checkout package version disagrees with release tag")


def replace_owned_updater(destination: Path, payload: bytes) -> None:
    """Owner-checked, backed-up, fsynced atomic replacement; no service changes."""
    if not destination.is_file() or destination.is_symlink():
        raise RuntimeError("Refusing to replace missing or symlinked standalone updater")
    old_stat = destination.stat()
    if (old_stat.st_uid != 0 or old_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise RuntimeError("Standalone updater must be root-owned and not group-writable")
    previous = destination.read_bytes()
    if IDENTITY not in previous[:512] or IDENTITY not in payload[:512]:
        raise RuntimeError("Unrecognized standalone updater source")
    ast.parse(payload.decode("utf-8"), filename=destination.name)
    if payload == previous:
        return
    backup = destination.with_name(destination.name + ".pre-mutable-backup")
    if os.path.lexists(backup):
        if not backup.is_file() or backup.is_symlink() or backup.stat().st_uid != 0:
            raise RuntimeError("Unsafe existing backup path")
    else:
        os.link(destination, backup, follow_symlinks=False)
    fd, temporary = tempfile.mkstemp(prefix=".devstack-updater-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fchmod(file.fileno(), 0o644)
            os.fsync(file.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(payload).digest():
        raise RuntimeError("Installed standalone updater did not match verified wheel")


def promote(root: Path = ROOT, host_root: Path = HOST_ROOT,
            destination: Path = DESTINATION) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Run using sudo (root is required)")
    assert_published_checkout(root)
    selected = host_root / "current"
    if not selected.is_symlink():
        raise RuntimeError("Current DevStack release symlink is missing")
    target = selected.resolve(strict=True)
    expected = host_root / "releases" / "v0.6.5"
    if target != expected or (target / ".ready").read_text().strip() != "v0.6.5":
        raise RuntimeError("Expected an already installed v0.6.5 bridge release")
    # Import updater from a checked, tagged checkout. This only downloads and
    # verifies the released wheel; it never executes an unverified artifact.
    sys.path.insert(0, str(root))
    from scripts import updater
    if not updater.healthy("http://127.0.0.1:8765/healthz", attempts=2):
        raise RuntimeError("Deactivate projects and restore controller health first")
    filename, data = updater.verified_wheel(REPO, TAG)
    if filename != "devstack-0.6.6-py3-none-any.whl":
        raise RuntimeError("Unexpected release wheel name")
    with ZipFile(io.BytesIO(data)) as archive:
        payload = archive.read(EMBEDDED)
    source = (root / "scripts" / "updater.py").read_bytes()
    if hashlib.sha256(payload).digest() != hashlib.sha256(source).digest():
        raise RuntimeError("Release wheel updater differs from the tagged checkout")
    # This is intentionally reviewed source from a published wheel, not a
    # hand-edited patch of the live privileged updater.
    replace_owned_updater(destination, payload)
    print("Standalone updater refreshed from verified v0.6.6 wheel; app unchanged.")


if __name__ == "__main__":
    try:
        promote()
    except Exception as exc:
        raise SystemExit(f"Updater migration refused: {exc}") from exc
