"""Native, unprivileged DevStack operator CLI.

All live state and operations are fetched through the existing controller IPC.
No Docker, subprocess, database, project workspace or sudo functionality lives
in the CLI. More project/runtime commands will be added in follow-up PRs.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from pathlib import Path
import re
import shutil
import sys
import textwrap
from uuid import UUID

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

# Receipts are controller-owned. Polling stops when the SSH client exits.
POLL_SECONDS = 0.25
RECEIPT_TIMEOUT = 10

_CONFLICT_CODES = frozenset({
    "TransitionConflict", "ControlError", "ProjectConflict", "JobError", "DockerControlError",
    "ConfigurationConflict", "WebhookBusy", "ImportConflict",
    "RepositoryGitError", "SourceSyncConflict", "RepositoryAddConflict",
    "BulkRepositoryConflict",
})


def _project_uuid(raw: str) -> str:
    """Reject ambiguous names and noncanonical IDs before controller IPC."""
    try:
        value = UUID(raw)
    except (TypeError, ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError("expected a canonical project UUIDv4") from exc
    if value.version != 4 or str(value) != raw:
        raise argparse.ArgumentTypeError("expected a canonical project UUIDv4")
    return raw


def _common_flags(command: argparse.ArgumentParser) -> None:
    """Allow global flags before or after any command, without clobbering them."""
    command.add_argument("--json", action="store_true",
                         default=argparse.SUPPRESS, help="Machine-readable JSON")
    command.add_argument("--socket", type=Path, default=argparse.SUPPRESS,
                         metavar="PATH", help=argparse.SUPPRESS)


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
        # Flags work before or after the command, including narrow SSH use.
        _common_flags(command)

    projects = commands.add_parser(
        "project", help="List, inspect and explicitly manage projects.",
        description="Project identity is an immutable UUIDv4; viewing never activates.")
    _common_flags(projects)
    actions = projects.add_subparsers(dest="action", metavar="ACTION", required=True)
    for name, desc in (
        ("list", "List registered projects without changing the runtime."),
        ("show", "Show a registered project's details without activating it."),
        ("activate", "Activate a project; requires the global runtime to be inactive."),
        ("switch", "Switch from the active project to a specified project."),
        ("deactivate", "Stop the globally active runtime; no project is deleted."),
    ):
        command = actions.add_parser(name, help=desc, description=desc)
        _common_flags(command)
        if name in {"show", "activate", "switch"}:
            command.add_argument("project_id", type=_project_uuid, metavar="UUID")
        if name in {"activate", "switch", "deactivate"}:
            _transition_flags(command)

    runtime = commands.add_parser(
        "runtime", help="Explicit global runtime recovery.",
        description="Reconciliation stops owned workloads; it does not reactivate any project.")
    _common_flags(runtime)
    runtime_actions = runtime.add_subparsers(dest="action", required=True)
    reconcile = runtime_actions.add_parser(
        "reconcile", help="Reconcile controller state without autoactivation.")
    _common_flags(reconcile)
    _transition_flags(reconcile)
    listing = runtime_actions.add_parser(
        "operations", help="List recent durable runtime transition receipts.")
    _common_flags(listing)
    operation = runtime_actions.add_parser(
        "operation", help="Inspect one durable transition by receipt UUID.")
    _common_flags(operation)
    operation.add_argument("id", type=_project_uuid, metavar="UUID")
    return root


def _transition_flags(command: argparse.ArgumentParser) -> None:
    command.add_argument("--detach", action="store_true",
                         help="Submit and return receipt immediately without following")
    command.add_argument("--request-key", type=_request_key, metavar="HEX32",
                         help="Reuse the same idempotency key after disconnect")


def _request_key(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise argparse.ArgumentTypeError("expected 32 lowercase hex characters")
    return value


def _width() -> int:
    return max(40, min(120, shutil.get_terminal_size(fallback=(80, 24)).columns))


def _line(label: str, value: object) -> str:
    text = str(value)
    prefix = label + ": "
    if len(prefix) + len(text) <= _width():
        return prefix + text
    # Keep UUIDs whole on 40-column mobile terminals.
    return label + ":\n" + textwrap.indent(
        textwrap.fill(text, width=_width() - 2, break_long_words=True,
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


def _render_project_list(data: dict) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        raise ControllerUnavailable("Malformed project list")
    if not data["projects"]:
        return "No projects registered."
    lines = []
    for item in data["projects"]:
        if not isinstance(item, dict):
            raise ControllerUnavailable("Malformed project list entry")
        for key in ("name", "slug", "id"):
            if not isinstance(item.get(key), str):
                raise ControllerUnavailable("Malformed project list entry")
        if lines:
            lines.append("")
        lines.extend((_line("Name", item["name"]),
                      _line("Slug", item["slug"]),
                      _line("ID", item["id"])))
    return "\n".join(lines)


def _render_project_detail(data: dict) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("project"), dict):
        raise ControllerUnavailable("Malformed project details")
    project = data["project"]
    for key in ("id", "slug", "name"):
        if not isinstance(project.get(key), str):
            raise ControllerUnavailable("Malformed project identity")
    lines = [
        _line("Name", project["name"]),
        _line("Slug", project["slug"]),
        _line("ID", project["id"]),
    ]
    linked = data.get("linked_source")
    if type(linked) is not bool:
        raise ControllerUnavailable("Malformed project source status")
    lines.append(_line("Linked source", "yes" if linked else "no"))
    for key, title in (
        ("repositories", "Repositories"),
        ("applications", "Applications"),
        ("tasks", "Tasks"),
    ):
        values = data.get(key)
        if not isinstance(values, list):
            raise ControllerUnavailable("Malformed project details")
        lines.append(_line(title, len(values)))
        for entry in values:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise ControllerUnavailable("Malformed project details entry")
            lines.append(_line("  -", entry["name"]))
    return "\n".join(lines)


def _render_receipt(data: dict) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        raise ControllerUnavailable("Invalid transition receipt")
    lines = [
        _line("Operation", data.get("id")),
        _line("Action", data.get("action")),
        _line("State", data["status"]),
    ]
    if data.get("project_id"):
        lines.append(_line("Project", data["project_id"]))
    if data.get("error_code"):
        lines.append(_line("Error", data["error_code"]))
    return "\n".join(lines)


def _render_receipt_list(data: dict) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("operations"), list):
        raise ControllerUnavailable("Invalid transition list")
    if not data["operations"]:
        return "No transitions recorded."
    return "\n\n".join(_render_receipt(item) for item in data["operations"])


def _print(payload: object, *, use_json: bool, name: str) -> None:
    if use_json:
        print(json.dumps(payload, separators=(",", ":"), ensure_ascii=True,
                         allow_nan=False))
    elif name in {"status", "project_activate", "project_switch",
                  "project_deactivate", "runtime_reconcile"}:
        print(_render_status(payload))
    elif name in {"transition_receipt", "transition_detached"}:
        print(_render_receipt(payload))
    elif name == "transition_list":
        print(_render_receipt_list(payload))
    elif name == "project_list":
        print(_render_project_list(payload))
    elif name == "project_show":
        print(_render_project_detail(payload))
    elif name == "doctor":
        version = payload["protocol"]
        print(_line("IPC", f"compatible ({version['major']}.{version['minor']})"))
        print(_render_status(payload["runtime"]))


def _is_transition(args: argparse.Namespace) -> bool:
    return ((args.command == "project" and args.action in
             {"activate", "switch", "deactivate"})
            or (args.command == "runtime" and args.action == "reconcile"))


def _unknown_outcome(key: str | None = None) -> None:
    message = ("dev: operation outcome is unknown; inspect 'dev runtime operations'"
               " or 'dev status' before retrying.")
    if key:
        message += f" Reuse --request-key {key} to recover the receipt."
    print(message, file=sys.stderr)


def _submit_transition(args: argparse.Namespace) -> tuple[object, str]:
    action = args.action
    project_id = (args.project_id if action in {"activate", "switch"} else None)
    key = args.request_key or secrets.token_hex(16)
    # Print before network submission. If SSH drops before receiving a receipt,
    # an operator can safely resubmit with this same key, never duplicate work.
    print(f"dev: transition request key {key}", file=sys.stderr, flush=True)
    receipt = send(
        args.socket, "transition_submit", action=action,
        project_id=project_id, request_key=key, timeout=RECEIPT_TIMEOUT,
    )
    if args.detach:
        return receipt, "transition_detached"
    return _await_transition(args.socket, receipt), "transition_result"


def _await_transition(socket: Path, receipt: dict) -> dict:
    if not isinstance(receipt, dict) or not isinstance(receipt.get("id"), str):
        raise ControllerUnavailable("Malformed runtime transition receipt")
    identifier = receipt["id"]
    while True:
        state = receipt.get("status")
        if state == "succeeded":
            result = receipt.get("result")
            if not isinstance(result, dict):
                raise ControllerUnavailable("Missing successful transition result")
            return result
        if state in {"failed", "interrupted"}:
            error = receipt.get("error_code")
            if not isinstance(error, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", error):
                error = "TransitionFailed"
            raise TransitionUnsuccessful(identifier, state, error)
        if state not in {"queued", "running"}:
            raise ControllerUnavailable("Malformed transition state")
        time.sleep(POLL_SECONDS)
        receipt = send(socket, "transition_status", id=identifier,
                       timeout=RECEIPT_TIMEOUT)


class TransitionUnsuccessful(RuntimeError):
    def __init__(self, identifier: str, status: str, error: str):
        self.identifier = identifier
        self.status = status
        self.error = error
        super().__init__(status)


def _dispatch(args: argparse.Namespace) -> tuple[object, str]:
    if args.command == "doctor":
        protocol = ensure_compatible(args.socket)
        status = send(args.socket, "status")
        return {"protocol": protocol, "runtime": status}, "doctor"
    min_version = (1 if (_is_transition(args) or args.command == "runtime")
                   else 0)
    ensure_compatible(args.socket, minimum_minor=min_version)
    if args.command == "status":
        return send(args.socket, "status"), "status"
    if args.command == "project":
        if args.action == "list":
            return send(args.socket, "projects"), "project_list"
        if args.action == "show":
            return send(args.socket, "project_detail", project_id=args.project_id), "project_show"
    if args.command == "runtime":
        if args.action == "operations":
            return send(args.socket, "transition_list"), "transition_list"
        if args.action == "operation":
            return send(args.socket, "transition_status", id=args.id), "transition_receipt"
    if _is_transition(args):
        return _submit_transition(args)
    raise AssertionError("Unexpected CLI dispatch")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    key = args.request_key if _is_transition(args) else None
    try:
        payload, name = _dispatch(args)
        if name == "transition_result":
            name = "status"
        _print(payload, use_json=args.json, name=name)
        return 0
    except TransitionUnsuccessful as exc:
        print(f"dev: transition {exc.identifier} {exc.status} ({exc.error}).",
              file=sys.stderr)
        if exc.status == "interrupted":
            print("dev: inspect 'dev status'; interrupted transitions are never auto-replayed.",
                  file=sys.stderr)
        return EX_TEMPFAIL
    except KeyboardInterrupt:
        if _is_transition(args):
            _unknown_outcome(key)
        else:
            print("dev: interrupted.", file=sys.stderr)
        return 130
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
        if _is_transition(args):
            _unknown_outcome(key)
        return EX_UNAVAILABLE
    except ControllerRejected as exc:
        code = exc.code if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", exc.code) else "Rejected"
        print(f"dev: controller rejected request ({code}).", file=sys.stderr)
        return EX_TEMPFAIL if code in _CONFLICT_CODES else EX_DATAERR
    except (ValueError, TypeError, UnicodeError, KeyError, AttributeError):
        print("dev: controller returned incompatible response data.", file=sys.stderr)
        return EX_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
