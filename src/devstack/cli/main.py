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

from devstack.cli import git_commands

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
JOB_POLL_SECONDS = 0.5
JOB_LOG_MAX_OFFSET = 2 * 1024 * 1024

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

    git_commands.register(actions, commands, _common_flags, _project_flag,
                          _project_uuid, _request_key)

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
    service = commands.add_parser(
        "service", help="Inspect project Compose services and bounded container logs.")
    _common_flags(service)
    services = service.add_subparsers(dest="action", required=True)
    service_list = services.add_parser("list", help="List services for an explicit project.")
    _common_flags(service_list)
    _project_flag(service_list)
    service_logs = services.add_parser(
        "logs", help="Read bounded logs from a verified project container.")
    _common_flags(service_logs)
    service_logs.add_argument("container_id", type=_container_id, metavar="CONTAINER_ID")
    _project_flag(service_logs)
    service_logs.add_argument("--tail", type=_tail, default=150, metavar="LINES",
                              help="Last 1–200 lines (default: 150)")

    task = commands.add_parser(
        "task", help="List and submit saved tasks through the controller.")
    _common_flags(task)
    tasks = task.add_subparsers(dest="action", required=True)
    task_list = tasks.add_parser("list", help="List saved task definitions (read-only).")
    _common_flags(task_list)
    _project_flag(task_list)
    task_run = tasks.add_parser("run", help="Submit a saved task as a controller job.")
    _common_flags(task_run)
    task_run.add_argument("name", type=_task_name, metavar="TASK_NAME")
    _project_flag(task_run)

    compose = commands.add_parser(
        "compose", help="Read managed Compose or submit safe maintenance jobs.")
    _common_flags(compose)
    actions = compose.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("show", "Read the managed Compose YAML and revision."),
        ("build", "Submit a Compose image build job."),
        ("restart", "Submit a Compose service restart job."),
    ):
        command = actions.add_parser(name, help=help_text)
        _common_flags(command)
        _project_flag(command)
    job = commands.add_parser(
        "job", help="Submit disposable jobs; inspect, follow and cancel jobs.")
    _common_flags(job)
    jobs = job.add_subparsers(dest="action", required=True)
    job_list = jobs.add_parser("list", help="List recent jobs for a project.")
    _common_flags(job_list)
    _project_flag(job_list)
    job_show = jobs.add_parser("show", help="Show one project's job status.")
    _common_flags(job_show)
    job_show.add_argument("id", type=_project_uuid, metavar="JOB_UUID")
    _project_flag(job_show)
    job_logs = jobs.add_parser(
        "logs", help="Read paginated job logs or follow until completion.")
    _common_flags(job_logs)
    job_logs.add_argument("id", type=_project_uuid, metavar="JOB_UUID")
    _project_flag(job_logs)
    job_logs.add_argument("--offset", type=_job_offset, default=0, metavar="BYTES")
    job_logs.add_argument("--follow", action="store_true",
                          help="Poll successive bounded chunks (Ctrl-C stops following only)")
    job_cancel = jobs.add_parser(
        "cancel", help="Cancel only an independently cancellable job.")
    _common_flags(job_cancel)
    job_cancel.add_argument("id", type=_project_uuid, metavar="JOB_UUID")
    _project_flag(job_cancel)
    for name, description in (
        ("run", "Submit disposable Compose run (no interactive stdin/TTY)."),
        ("image", "Submit a standalone Docker image job."),
    ):
        command = jobs.add_parser(name, help=description)
        _common_flags(command)
        command.add_argument("target", type=_task_name if name == "run"
                             else _image_name, metavar="SERVICE" if name == "run"
                             else "IMAGE")
        _project_flag(command)
        if name == "image":
            command.add_argument("--network", choices=("none", "project"),
                                 default="none", help="Container network isolation")
    return root


def _project_flag(command: argparse.ArgumentParser) -> None:
    command.add_argument("--project", dest="project_id", type=_project_uuid,
                         metavar="UUID", required=True,
                         help="Explicit project UUID; never inferred from active state")


def _container_id(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise argparse.ArgumentTypeError("expected a full 64-character Docker container ID")
    return value


def _image_name(value: str) -> str:
    if (len(value) > 255 or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_./:@-]*", value)):
        raise argparse.ArgumentTypeError("invalid container image reference")
    return value


def _job_offset(value: str) -> int:
    try:
        result = int(value)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("job offset must be a byte position") from exc
    if str(result) != value or not 0 <= result <= JOB_LOG_MAX_OFFSET:
        raise argparse.ArgumentTypeError("job offset must be between 0 and 2097152")
    return result


def _tail(value: str) -> int:
    try:
        count = int(value)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("tail must be 1–200") from exc
    if not 1 <= count <= 200:
        raise argparse.ArgumentTypeError("tail must be 1–200")
    return count


def _task_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value) or ".." in value:
        raise argparse.ArgumentTypeError("invalid saved task name")
    return value


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


def _render_services(data: dict) -> str:
    if (not isinstance(data, dict) or type(data.get("active")) is not bool
            or not isinstance(data.get("services"), list)):
        raise ControllerUnavailable("Malformed service response")
    if not data["active"]:
        return "Runtime inactive for this project."
    if not data["services"]:
        return "No Compose services."
    lines = []
    for service in data["services"]:
        if (not isinstance(service, dict)
                or not isinstance(service.get("name"), str)
                or not isinstance(service.get("status"), str)
                or not isinstance(service.get("containers"), list)):
            raise ControllerUnavailable("Malformed service metadata")
        if lines:
            lines.append("")
        lines.extend((_line("Service", service["name"]),
                      _line("State", service["status"])))
        if not service["containers"]:
            lines.append("  No containers.")
        for container in service["containers"]:
            if (not isinstance(container, dict)
                    or not isinstance(container.get("id"), str)
                    or not isinstance(container.get("state"), str)):
                raise ControllerUnavailable("Malformed container metadata")
            lines.extend((_line("Container", container["id"]),
                          _line("Status", container["state"])))
            if container.get("health"):
                lines.append(_line("Health", container["health"]))
    return "\n".join(lines)


def _render_tasks(data: dict) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ControllerUnavailable("Malformed task definitions")
    if not data["tasks"]:
        return "No saved tasks."
    lines = []
    for task in data["tasks"]:
        if (not isinstance(task, dict)
                or not isinstance(task.get("name"), str)
                or not isinstance(task.get("mode"), str)):
            raise ControllerUnavailable("Malformed saved task")
        if lines:
            lines.append("")
        lines.extend((_line("Task", task["name"]), _line("Mode", task["mode"])))
        if task.get("service"):
            lines.append(_line("Service", task["service"]))
        if task.get("image"):
            lines.append(_line("Image", task["image"]))
    return "\n".join(lines)


def _render_job_submission(data: dict) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("job_id"), str):
        raise ControllerUnavailable("Malformed job submission result")
    return _line("Job", data["job_id"])


def _render_compose(data: dict) -> str:
    if (not isinstance(data, dict) or not isinstance(data.get("content"), str)
            or not isinstance(data.get("revision"), str)):
        raise ControllerUnavailable("Malformed Compose configuration")
    # Compose is a source file: preserve exact YAML; don't reflow whitespace.
    return data["content"]


def _render_service_logs(data: dict) -> str:
    if (not isinstance(data, dict) or not isinstance(data.get("text"), str)
            or type(data.get("truncated")) is not bool
            or type(data.get("tail")) is not int):
        raise ControllerUnavailable("Malformed service logs")
    # Keep raw log lines/timestamps intact; the controller already bounds bytes.
    return data["text"]


_JOB_STATES = frozenset({
    "queued", "running", "cancelling", "succeeded", "failed", "cancelled",
})


def _validate_job(data: object) -> dict:
    if (not isinstance(data, dict) or not isinstance(data.get("id"), str)
            or not isinstance(data.get("project_id"), str)
            or data.get("status") not in _JOB_STATES
            or not isinstance(data.get("kind"), str)
            or not isinstance(data.get("action"), str)
            or data.get("cancel_policy") not in {"project", "independent"}):
        raise ControllerUnavailable("Malformed controller job response")
    return data


def _render_job(data: dict) -> str:
    job = _validate_job(data)
    parts = [
        _line("Job", job["id"]),
        _line("State", job["status"]),
        _line("Kind", job["kind"]),
        _line("Action", job["action"]),
        _line("Cancel", job["cancel_policy"]),
    ]
    if job.get("exit_code") is not None:
        if type(job["exit_code"]) is not int:
            raise ControllerUnavailable("Malformed job exit status")
        parts.append(_line("Exit code", job["exit_code"]))
    return "\n".join(parts)


def _render_jobs(data: dict) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ControllerUnavailable("Malformed controller job list")
    if not data["jobs"]:
        return "No jobs recorded for this project."
    return "\n\n".join(_render_job(job) for job in data["jobs"])


def _validate_job_logs(data: object, *, expected_offset: int) -> dict:
    if (not isinstance(data, dict) or not isinstance(data.get("text"), str)
            or type(data.get("offset")) is not int
            or type(data.get("next_offset")) is not int
            or type(data.get("complete")) is not bool
            or data["offset"] != expected_offset
            or not expected_offset <= data["next_offset"] <= JOB_LOG_MAX_OFFSET
            or len(data["text"].encode("utf-8")) > 16 * 1024 * 3):
        raise ControllerUnavailable("Malformed or out-of-order controller job logs")
    return data


def _follow_job_logs(socket: Path, project: str, identifier: str,
                     offset: int, *, use_json: bool) -> None:
    """Follow controller-owned bounded chunks, never own/cancel the job.

    JSON + follow uses newline-delimited JSON (NDJSON), not one JSON document.
    Ctrl-C exits the follower while the underlying job continues independently.
    """
    while True:
        payload = _validate_job_logs(
            send(socket, "job_logs", project_id=project, id=identifier, offset=offset),
            expected_offset=offset,
        )
        if use_json:
            if payload["text"] or payload["complete"]:
                print(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
                sys.stdout.flush()
        elif payload["text"]:
            sys.stdout.write(payload["text"])
            sys.stdout.flush()
        offset = payload["next_offset"]
        if payload["complete"]:
            return
        time.sleep(JOB_POLL_SECONDS)


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
    elif name == "service_list":
        print(_render_services(payload))
    elif name == "service_logs":
        print(_render_service_logs(payload), end="")
    elif name == "task_list":
        print(_render_tasks(payload))
    elif name in {"task_run", "compose_build", "compose_restart"}:
        print(_render_job_submission(payload))
    elif name == "compose_show":
        print(_render_compose(payload), end="")
    elif name in {"repo_list", "repo_status", "git_operation",
                  "branch_preview", "branch_apply"}:
        print(git_commands.render(name, payload, _line))
    elif name == "job_list":
        print(_render_jobs(payload))
    elif name in {"job_show", "job_cancel"}:
        print(_render_job(payload))
    elif name == "job_logs":
        logs = _validate_job_logs(payload, expected_offset=payload.get("offset", -1))
        print(logs["text"], end="")
    elif name in {"job_run", "job_image"}:
        print(_render_job_submission(payload))
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
    if args.command == "repo" or (args.command == "project" and args.action
                                  in {"import", "import-status"}):
        return git_commands.dispatch(args, send)
    if args.command == "project":
        if args.action == "list":
            return send(args.socket, "projects"), "project_list"
        if args.action == "show":
            return send(args.socket, "project_detail", project_id=args.project_id), "project_show"
    if args.command == "service":
        if args.action == "list":
            return send(args.socket, "services",
                        project_id=args.project_id), "service_list"
        if args.action == "logs":
            return send(args.socket, "service_logs", project_id=args.project_id,
                        container_id=args.container_id, tail=args.tail), "service_logs"
    if args.command == "task":
        if args.action == "list":
            settings = send(args.socket, "settings_read", project_id=args.project_id)
            return settings, "task_list"
        if args.action == "run":
            return send(args.socket, "task", project_id=args.project_id,
                        name=args.name), "task_run"
    if args.command == "compose":
        if args.action == "show":
            return send(args.socket, "compose_read",
                        project_id=args.project_id), "compose_show"
        if args.action in {"build", "restart"}:
            return send(args.socket, "compose", project_id=args.project_id,
                        action=args.action), "compose_" + args.action
    if args.command == "job":
        if args.action == "list":
            return send(args.socket, "jobs", project_id=args.project_id), "job_list"
        if args.action == "show":
            return send(args.socket, "job", project_id=args.project_id,
                        id=args.id), "job_show"
        if args.action == "logs":
            return send(args.socket, "job_logs", project_id=args.project_id,
                        id=args.id, offset=args.offset), "job_logs"
        if args.action == "cancel":
            return send(args.socket, "job_cancel", project_id=args.project_id,
                        id=args.id), "job_cancel"
        if args.action in {"run", "image"}:
            return send(
                args.socket, "adhoc", project_id=args.project_id,
                kind=args.action, service=args.target if args.action == "run" else "",
                image=args.target if args.action == "image" else "",
                network=args.network if args.action == "image" else "none",
                command=args.command_argv,
            ), "job_" + args.action
    if args.command == "runtime":
        if args.action == "operations":
            return send(args.socket, "transition_list"), "transition_list"
        if args.action == "operation":
            return send(args.socket, "transition_status", id=args.id), "transition_receipt"
    if _is_transition(args):
        return _submit_transition(args)
    raise AssertionError("Unexpected CLI dispatch")


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    cli_parser = parser()
    # Ad-hoc command argv is never parsed as host/DevStack flags. A literal
    # separator is required; it preserves arguments like '-c' and '--version'.
    suffix = None
    if "--" in raw:
        boundary = raw.index("--")
        candidate = cli_parser.parse_args(raw[:boundary])
        if candidate.command != "job" or candidate.action not in {"run", "image"}:
            cli_parser.error("'--' is only supported for ad-hoc job commands")
        suffix = raw[boundary + 1:]
        args = candidate
    else:
        args = cli_parser.parse_args(raw)
    if args.command == "job" and args.action in {"run", "image"}:
        if not suffix or len(suffix) > 64 or any(
                not part or "\x00" in part for part in suffix
        ) or sum(len(part) for part in suffix) > 4096:
            cli_parser.error("ad-hoc jobs need -- followed by 1–64 bounded argv items")
        args.command_argv = suffix
    key = args.request_key if _is_transition(args) else None
    try:
        if args.command == "job" and args.action == "logs" and args.follow:
            ensure_compatible(args.socket)
            _follow_job_logs(args.socket, args.project_id, args.id, args.offset,
                             use_json=args.json)
            return 0
        payload, name = _dispatch(args)
        if name == "transition_result":
            name = "status"
        _print(payload, use_json=args.json, name=name)
        return 0
    except git_commands.GitOperationUnsuccessful as exc:
        status = exc.receipt.get("status")
        identifier = exc.receipt.get("id")
        print(f"dev: Git operation {identifier} ended as {status}; inspect its status receipt.",
              file=sys.stderr)
        return EX_TEMPFAIL
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
        elif args.command == "repo" or (args.command == "project" and args.action == "import"):
            print("dev: stopped waiting. Git work may continue; inspect operation status.",
                  file=sys.stderr)
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
