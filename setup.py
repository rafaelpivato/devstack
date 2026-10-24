"""Refuse installation from source-only release archives without built React assets.

Old v0.6.4 standalone updaters clone tags and run pip on the checkout.
A source-only v0.6.6 tag must fail during wheel construction rather than
silently install a Python package with a missing dashboard.
"""
import json
import re
from pathlib import Path

from setuptools import setup

root = Path(__file__).resolve().parent
bundle = root / "src/devstack/static/v06"
manifest_path = bundle / "manifest.json"
if not manifest_path.is_file():
    raise RuntimeError(
        "Missing built React assets: DevStack release installs require a "
        "CI-produced wheel. Upgrade via v0.6.5 bridge and refresh updater."
    )

try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if "src/main.tsx" not in manifest:
        raise ValueError("Missing React entry")
    for entry in manifest.values():
        assets = ([entry["file"]] if "file" in entry else [])
        assets += entry.get("css", []) + entry.get("assets", [])
        for asset in assets:
            if not re.fullmatch(r"assets/[A-Za-z0-9._/-]+", asset):
                raise ValueError("Invalid React asset path")
            if ".." in asset.split("/") or not (bundle / asset).is_file():
                raise ValueError("React asset missing from build")
except (OSError, KeyError, ValueError, TypeError) as exc:
    raise RuntimeError("Incomplete or invalid React build; release wheel required") from exc

setup()
