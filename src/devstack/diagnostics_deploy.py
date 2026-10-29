"""Idempotent, root-owned repair of legacy Web diagnostics systemd state.

Older installs keep their original Web unit across wheel upgrades.  That unit
predates StateDirectory=devstack-web, so ProtectSystem=strict prevents writes
to the otherwise existing /var/lib/devstack-web.  Only the trusted release
post-install hook invokes this; no Web/IPC endpoint exposes file operations.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile

WEB_UNIT = Path("/etc/systemd/system/devstack-web-v2.service")
WEB_DROPIN = Path("/etc/systemd/system/devstack-web-v2.service.d/10-diagnostics-state.conf")
MARKER = "# Managed by DevStack: persistent Web diagnostics\n"


def _root_owned(path: Path, *, directory: bool) -> None:
    info = path.lstat()  # Refuse symlinks, even if their destination looks safe.
    if (info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not (stat.S_ISDIR(info.st_mode) if directory
                    else stat.S_ISREG(info.st_mode))):
        raise RuntimeError("Untrusted diagnostics systemd path")


def ensure_state_directory(
    unit: Path = WEB_UNIT, dropin: Path = WEB_DROPIN,
    state_directory: str = "devstack-web",
) -> bool:
    """Install a known, minimal drop-in. Return whether the unit changed.

    Optional file/name arguments are for an isolated systemd integration test,
    not a runtime or operator API.
    """
    if os.geteuid() != 0:
        raise RuntimeError("Root required for diagnostics unit repair")
    unit, dropin = Path(unit), Path(dropin)
    if (unit.name not in ("devstack-web-v2.service", "devstack-ci-web.service")
            or dropin.name != "10-diagnostics-state.conf"
            or dropin.parent != unit.with_name(unit.name + ".d")
            or state_directory not in ("devstack-web", "devstack-ci-web")
            or (unit.name == "devstack-ci-web.service") !=
               (state_directory == "devstack-ci-web")):
        raise ValueError("Unrecognized diagnostics systemd target")
    _root_owned(unit, directory=False)
    _root_owned(unit.parent, directory=True)
    if not dropin.parent.exists():
        dropin.parent.mkdir(mode=0o755)
    _root_owned(dropin.parent, directory=True)

    content = (MARKER + "[Service]\n"
               + f"StateDirectory={state_directory}\n"
               + "StateDirectoryMode=0700\n")
    if dropin.exists() or dropin.is_symlink():
        _root_owned(dropin, directory=False)
        previous = dropin.read_text(encoding="utf-8")
        if previous == content:
            return False
        if not previous.startswith(MARKER):
            raise RuntimeError("Refusing to overwrite unrelated systemd drop-in")

    fd, temporary = tempfile.mkstemp(prefix=".devstack-diagnostics-",
                                    dir=dropin.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(temporary, dropin)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    return True
