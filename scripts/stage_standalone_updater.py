#!/usr/bin/env python3
"""Stage the canonical standalone updater into the build-only package resource."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/updater.py"
DESTINATION = ROOT / "src/devstack/_standalone_updater.py"


def stage() -> None:
    data = SOURCE.read_bytes()
    if b"Independent, conservative release updater" not in data[:512]:
        raise RuntimeError("Unrecognized standalone updater source")
    DESTINATION.write_bytes(data)
    if DESTINATION.read_bytes() != data:
        raise RuntimeError("Staged updater verification failed")


if __name__ == "__main__":
    stage()
