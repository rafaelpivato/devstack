"""One-time, root-controlled registration of the versioned 'dev' CLI.

The symlink points through /opt/devstack/current so updater pointer swaps
and rollbacks also switch CLI versions. Never replace an unrelated command.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess

COMMAND = "dev"
DEFAULT_ROOT = Path("/opt/devstack")
DEFAULT_DESTINATION = Path("/usr/local/bin/dev")
GROUP = "devstackipc"


class CLIInstallError(RuntimeError):
    """Unsafe or incomplete local CLI installation."""


def install_command(root: Path = DEFAULT_ROOT,
                    destination: Path = DEFAULT_DESTINATION) -> bool:
    """Install a collision-safe symlink. Return True only if newly installed.

    Used only by the root-controlled installer/updater or explicit operator
    setup. Never writes through an existing symlink, file or directory.
    """
    root = Path(root)
    destination = Path(destination)
    target = root / "current" / "venv" / "bin" / COMMAND
    if not target.is_file() or not os.access(target, os.X_OK):
        raise CLIInstallError("The selected DevStack release does not provide 'dev'")
    if destination.is_symlink() and os.readlink(destination) == str(target):
        return False
    # A different 'dev' found on PATH would be shadowed by the new shortcut.
    existing = shutil.which(COMMAND)
    if existing and Path(existing) != destination:
        raise CLIInstallError(
            f"Refusing to shadow existing command at {existing}"
        )
    if os.path.lexists(destination):
        raise CLIInstallError(
            f"Refusing to replace existing command at {destination}"
        )
    if not destination.parent.is_dir():
        raise CLIInstallError("CLI command directory is missing")
    # os.symlink fails if an unrelated command races us into the destination.
    os.symlink(target, destination)
    return True


def authorize_operator(username: str) -> None:
    """Explicitly enroll only a real trusted human SSH account."""
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}\$?", username):
        raise CLIInstallError("Invalid Linux username")
    try:
        account = pwd.getpwnam(username)
    except KeyError as exc:
        raise CLIInstallError("Linux operator account does not exist") from exc
    if (account.pw_uid < 1000 or account.pw_uid == 65534
            or account.pw_shell.endswith(("nologin", "/false"))
            or username in {"devstack", "devstackctl"}):
        raise CLIInstallError("Only trusted interactive human SSH accounts can be enrolled")
    subprocess.run(["usermod", "-aG", GROUP, username],
                   check=True, timeout=15)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user", metavar="SSH_USER",
        help="Explicitly authorize an existing trusted SSH user for full DevStack administration",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help=argparse.SUPPRESS)  # For CI/development release paths.
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("root is required for CLI setup (use sudo)")
    try:
        created = install_command(root=args.root)
        if args.user:
            authorize_operator(args.user)
    except (CLIInstallError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"dev: setup failed: {exc}\n")
    print("dev: command ready" + (" (already installed)" if not created else ""))
    if args.user:
        print("dev: operator authorized; log out and reconnect via SSH "
              "to refresh Unix groups")
    else:
        print("dev: no human SSH account was granted access; use --user SSH_USER "
              "for an explicitly trusted operator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
