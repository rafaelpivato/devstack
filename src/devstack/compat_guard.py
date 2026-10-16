"""Reject v0.4 installations before any v0.5 startup or reconciliation.

Existing v0.4 release updaters cannot be remotely patched. This guard executes
in the *candidate* v0.5 service's ExecStartPre, making a v0.4 -> v0.5
replacement fail its health check and roll back instead of loading old data.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

from .workspace import config_path, load_project


class IncompatibleInstallation(RuntimeError):
    """A legacy or invalid workspace needs an explicit clean installation."""


def check_installation(home: Path | None = None, config: Path | None = None) -> None:
    home = Path.home() if home is None else Path(home)
    legacy = home / ".config" / "devstack" / "projects.toml"
    if legacy.exists() or legacy.is_symlink():
        raise IncompatibleInstallation(
            "Legacy v0.4 projects.toml found. v0.5 requires a fresh Ubuntu installation; "
            "no in-place migration is supported."
        )
    target = config_path() if config is None else Path(config)
    if target.name == "projects.toml":
        raise IncompatibleInstallation(
            "DEVSTACK_CONFIG points to the unsupported v0.4 projects.toml format."
        )
    try:
        load_project(target)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise IncompatibleInstallation(
            "Invalid v0.5 workspace configuration; refusing to start."
        ) from exc


def main() -> int:
    try:
        check_installation()
        return 0
    except IncompatibleInstallation as exc:
        print(f"DevStack compatibility check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
