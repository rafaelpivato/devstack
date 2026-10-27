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
import stat
import sys
import subprocess
import tempfile

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
    if (existing and Path(existing) != destination
            and Path(existing).resolve() != target.resolve()):
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



def refresh_updater(root: Path = DEFAULT_ROOT,
                    destination: Path = Path("/usr/local/lib/devstack/updater.py")) -> bool:
    """Atomically install the trusted versioned updater on pre-CLI hosts.

    The v0.5.2 updater was installed outside the release directory and
    does not self-update. Only an explicit root operator command uses this
    migration; no background process fetches or executes an arbitrary file.
    """
    root, destination = Path(root), Path(destination)
    source = root / "current" / "source" / "scripts" / "updater.py"
    if not source.is_file():
        # Wheel-only releases carry this same standalone stdlib updater.
        # Refuse a caller running outside the selected release's venv.
        selected_venv = (root / "current" / "venv").resolve(strict=True)
        if Path(sys.prefix).resolve() != selected_venv:
            raise CLIInstallError("Updater refresh requires the selected release's venv")
        source = Path(__file__).resolve().parents[1] / "_standalone_updater.py"
    if not source.is_file() or source.is_symlink():
        raise CLIInstallError("Trusted release updater source is unavailable")
    if not destination.is_file() or destination.is_symlink():
        raise CLIInstallError("Refusing to replace an unrecognized updater path")
    old_stat = destination.stat()
    if (old_stat.st_uid != 0 or old_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise CLIInstallError("Installed updater is not root-owned and private from writes")
    content = source.read_bytes()
    previous = destination.read_bytes()
    marker = b"Independent, conservative release updater"
    if marker not in content[:512] or marker not in previous[:512]:
        raise CLIInstallError("Installed or release updater lacks trusted identity")
    if content == previous:
        return False
    # Save exactly one original updater for recovery, never overwrite backup.
    backup = destination.with_name(destination.name + ".pre-cli-backup")
    if not os.path.lexists(backup):
        try:
            os.link(destination, backup, follow_symlinks=False)
        except FileExistsError:
            pass  # Concurrent setup already saved its backup.
    fd, temporary = tempfile.mkstemp(prefix=".devstack-updater-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user", metavar="SSH_USER",
        help="Explicitly authorize an existing trusted SSH user for full DevStack administration",
    )
    parser.add_argument("--refresh-updater", action="store_true",
                        help="Refresh the independent root-owned updater from the trusted versioned release")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help=argparse.SUPPRESS)  # For CI/development release paths.
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("root is required for CLI setup (use sudo)")
    try:
        created = install_command(root=args.root)
        if args.root == DEFAULT_ROOT:
            # The previously installed (v0.7.2) updater invokes THIS wheel's
            # hook even though that updater knows nothing about networking.
            # Bootstrap dedicated public group/units, then restart controller
            # so its restricted ingress socket inherits the new group.
            from devstack.networking import prepare, reconcile
            prepare()
            subprocess.run(["systemctl", "restart", "devstack-control.service"],
                           check=True, timeout=45)
            from devstack.mcp_management import install_units
            install_units()
            # A temporary Tailscale failure is not an app rollback: the
            # reconcile timer retries without exposing the private dashboard.
            try:
                reconcile()
            except Exception as exc:
                print("dev: network reconciliation pending: "
                      + type(exc).__name__, file=sys.stderr, flush=True)
            try:
                subprocess.run(["systemctl", "start",
                                "devstack-network-reconcile.timer"],
                               check=True, timeout=20)
            except (OSError, subprocess.SubprocessError):
                print("dev: network retry timer unavailable",
                      file=sys.stderr, flush=True)
        if args.user:
            authorize_operator(args.user)
        # Release-owned updater refresh is safe after a verified wheel
        # install and keeps subsequent unattended updates migration-aware.
        refreshed = refresh_updater(args.root) if (
            args.refresh_updater or args.root == DEFAULT_ROOT) else False
    except (CLIInstallError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"dev: setup failed: {exc}\n")
    print("dev: command ready" + (" (already installed)" if not created else ""))
    if args.refresh_updater:
        print("dev: standalone updater ready" +
              (" (already current)" if not refreshed else " (migrated with backup)"))
    if args.user:
        print("dev: operator authorized; log out and reconnect via SSH "
              "to refresh Unix groups")
    else:
        print("dev: no human SSH account was granted access; use --user SSH_USER "
              "for an explicitly trusted operator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
