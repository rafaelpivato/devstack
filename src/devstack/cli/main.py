"""Native, unprivileged DevStack operator CLI.

All live state and operations are fetched through the existing controller IPC.
No Docker, subprocess, database, project workspace or sudo functionality lives
in the CLI. More project/runtime commands will be added in follow-up PRs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import textwrap

from devstack.ipc.client import (
    ControllerIncompatible, ControllerRejected, ControllerUnavailable,
    ensure_compatible, send,
)

DEFAULT_SOCKET = Path("/run/devstack-control/control.sock")
# Consistent sysexits-inspired categories; argparse reserves 2 for bad syntax.
EX_USAGE = 2
EX_DATAERR = 65
EX_UNAVAILABLE = 69
EX_TEMPFAIL = 75
EX_NOPERM = 77
EX_CONFIG = 78

_CONFLICT_CODES = frozenset({
    "ControlError", "ProjectConflict", "JobError", "DockerControlError",
    "ConfigurationConflict", "WebhookBusy", "ImportConflict",
    "RepositoryGitError", "SourceSyncConflict", "RepositoryAddConflict",
    "BulkRepositoryConflict",
})


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="dev",
        description="Control DevStack over its private local Unix socket.",
        epilog="Access requires an explicitly authorized DevStack operator account.",
    )
    root.add_argument(
        "--socket", type=Path,
        default=Path(os.environ.get("DEVSTACK_CONTROL_SOCKET", str(DEFAULT_SOCKET))),
        metavar="PATH", help="Controller Unix socket (for diagnostics/testing)",
    )
    root.add_argument(
        "--json", action="store_true",
        help="Output machine-readable JSON (no status commentary)",
    )
    commands = root.add_subparsers(dest="command", metavar="COMMAND")
    commands.required = True

    for name, description in (
        ("status", "Show global runtime state without changing it."),
        ("doctor", "Check controller IPC compatibility and availability."),
    ):
        command = commands.add_parser(name, help=description, description=description)
        # Support both 'dev --json status' and 'dev status --json'.
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        command.add_argument("--socket", type=Path, default=argparse.SUPPRESS,
                             metavar="PATH", help=argparse.SUPPRESS)
    return root


def _width() -> int:
    return max(40, min(120, shutil.get_terminal_size(fallback=(80, 24)).columns))


def _line(label: str, value: object) -> str:
    text = str(value)
    prefix = label + ": "
    if len(prefix) + len(text) <= _width():
        return prefix + text
    # Keep UUIDs whole on 40-column mobile terminals.
    return label + ":\n" + textwrap.indent(
        textwrap.fill(text, width=_width() - 2, break_long_words=False,
                      break_on_hyphens=False),
        "  ",
    )


def _render_status(data: dict) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("phase"), str):
        raise ControllerUnavailable("Malformed controller status")
    lines = [_line("Runtime", data["phase"])]
    if data.get("project_id"):
        lines.append(_line("Project", data["project_id"]))
    if data.get("message"):
        lines.append(_line("Details", data["message"]))
    return "\n".join(lines)


def _print(payload: object, *, use_json: bool, name: str) -> None:
    if use_json:
        print(json.dumps(payload, separators=(",", ":"), ensure_ascii=True,
                         allow_nan=False))
    elif name == "status":
        print(_render_status(payload))
    elif name == "doctor":
        version = payload["protocol"]
        print(_line("IPC", f"compatible ({version['major']}.{version['minor']})"))
        print(_render_status(payload["runtime"]))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        protocol = ensure_compatible(args.socket)
        if args.command == "doctor":
            status = send(args.socket, "status")
            _print({"protocol": protocol, "runtime": status},
                   use_json=args.json, name=args.command)
        elif args.command == "status":
            status = send(args.socket, "status")
            _print(status, use_json=args.json, name=args.command)
        else:
            # Every command is registered through argparse; no raw IPC input.
            raise AssertionError("Unrecognized CLI command")
        return 0
    except ControllerIncompatible:
        print("dev: controller IPC version is incompatible; upgrade DevStack.",
              file=sys.stderr)
        return EX_CONFIG
    except ControllerUnavailable as exc:
        if isinstance(exc.__cause__, PermissionError):
            print("dev: controller access denied. Ask an administrator to authorize "
                  "your SSH account in devstackipc, then log in again.",
                  file=sys.stderr)
            return EX_NOPERM
        print("dev: controller is unavailable or returned an invalid response.",
              file=sys.stderr)
        return EX_UNAVAILABLE
    except ControllerRejected as exc:
        code = exc.code if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", exc.code) else "Rejected"
        print(f"dev: controller rejected request ({code}).", file=sys.stderr)
        return EX_TEMPFAIL if code in _CONFLICT_CODES else EX_DATAERR
    except (ValueError, TypeError, UnicodeError, KeyError):
        print("dev: controller returned incompatible response data.", file=sys.stderr)
        return EX_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
