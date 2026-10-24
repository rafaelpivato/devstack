"""Safe GitHub/repository CLI workflows over the existing controller IPC.

No local Git subprocess, credentials, worktree, database or Docker access.
Every mutation is explicitly scoped and checked by the privileged controller.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time

POLL_SECONDS = 0.5
_OPERATION_STATUS = {
    "import": ("import_status", "id"),
    "repo_add": ("repository_add_status", "id"),
    "repo_bulk": ("repository_bulk_status", "id"),
}
_TERMINAL = {"succeeded", "failed", "interrupted", "partial", "blocked", "recovery"}
_SUCCESS = {"succeeded"}


class GitOperationUnsuccessful(RuntimeError):
    def __init__(self, receipt: dict):
        self.receipt = receipt
        super().__init__("Git operation did not succeed")


def _slug(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
        raise argparse.ArgumentTypeError("expected a project slug (lowercase letters, digits, hyphens)")
    return value


def _name(value: str) -> str:
    if not value or len(value) > 120 or value.strip() != value or any(
        ord(c) < 32 or ord(c) == 127 for c in value
    ):
        raise argparse.ArgumentTypeError("invalid display name")
    return value


def _repo_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value) or ".." in value:
        raise argparse.ArgumentTypeError("invalid repository name")
    return value


def _url(value: str) -> str:
    # The controller remains authoritative for all source/host/credentials
    # rules; client-side bounds and control-char checks avoid terminal leaks.
    if (not value or len(value) > 2048
            or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise argparse.ArgumentTypeError("invalid GitHub source URL")
    return value


def _branch(value: str) -> str:
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,239}", value)
            or ".." in value or "//" in value
            or value.endswith(("/", ".", ".lock"))):
        raise argparse.ArgumentTypeError("invalid Git branch")
    return value


def _sha(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{40}", value):
        raise argparse.ArgumentTypeError("expected a 40-character Git HEAD hash")
    return value


def _revision(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise argparse.ArgumentTypeError("expected 64-character reviewed revision")
    return value


def _add_wait(parser: argparse.ArgumentParser, request_key):
    parser.add_argument("--request-key", type=request_key, metavar="HEX32",
                        help="Reuse key after SSH disconnect to retrieve same operation")
    parser.add_argument("--wait", action="store_true",
                        help="Poll controller-owned operation until a final result")


def register(project_actions, root_commands, common, project_flag, uuid, request_key):
    project_import = project_actions.add_parser(
        "import", help="Import a GitHub project through the controller.")
    common(project_import)
    project_import.add_argument("url", type=_url, metavar="GITHUB_URL")
    project_import.add_argument("--slug", type=_slug, required=True)
    project_import.add_argument("--name", type=_name, required=True)
    _add_wait(project_import, request_key)

    import_status = project_actions.add_parser(
        "import-status", help="Inspect a prior GitHub project import.")
    common(import_status)
    import_status.add_argument("id", type=uuid, metavar="OPERATION_UUID")

    repo = root_commands.add_parser(
        "repo", help="Safe repository status, updates, adds and branch previews.")
    common(repo)
    actions = repo.add_subparsers(dest="action", required=True)
    for action, description in (
        ("list", "List repositories registered to an explicit project."),
        ("status", "Read a repository's Git HEAD/branch and dirty state."),
        ("update", "Safely fast-forward one repository from an observed HEAD."),
        ("check", "Fetch remote refs without updating working files."),
        ("sync-policy", "Configure opt-in checks or safe auto-fast-forward."),
        ("update-all", "Submit a controller-owned batch fast-forward."),
        ("update-status", "Inspect a controller-owned batch update receipt."),
        ("add", "Add a GitHub repository to an inactive project."),
        ("add-status", "Inspect a previously submitted repository add."),
        ("branch-preview", "Review branch changes, without applying any."),
        ("branch-apply", "Apply an explicitly reviewed branch revision."),
    ):
        command = actions.add_parser(action, help=description)
        common(command)
        project_flag(command)
        if action in {"status", "update", "check", "sync-policy"}:
            command.add_argument("repository_id", type=uuid, metavar="REPO_UUID")
        if action == "update":
            command.add_argument("--expected-head", type=_sha, required=True,
                                 metavar="SHA40")
        if action == "sync-policy":
            command.add_argument("--mode", choices=("manual", "check", "update"), required=True)
        if action == "add":
            command.add_argument("url", type=_url, metavar="GITHUB_URL")
            command.add_argument("--name", type=_repo_name, required=True)
            command.add_argument("--branch", type=_branch, default="",
                                 help="Remote branch (default: repository's default)")
        if action in {"update-all", "add"}:
            _add_wait(command, request_key)
        if action in {"update-status", "add-status"}:
            command.add_argument("id", type=uuid, metavar="OPERATION_UUID")
        if action in {"branch-preview", "branch-apply"}:
            command.add_argument("branch", type=_branch, metavar="BRANCH")
            command.add_argument("--repository", type=uuid, default=None,
                                 metavar="REPO_UUID",
                                 help="Scope to one repo (default: all project repos)")
        if action == "branch-apply":
            command.add_argument("--revision", type=_revision, required=True,
                                 metavar="SHA256")


def _wait(sender, socket, receipt: dict, kind: str, project: str | None) -> dict:
    op, id_field = _OPERATION_STATUS[kind]
    identifier = receipt.get("id")
    if not isinstance(identifier, str):
        raise ValueError("Malformed Git operation receipt")
    while True:
        status = receipt.get("status")
        if status in _TERMINAL:
            if status not in _SUCCESS:
                raise GitOperationUnsuccessful(receipt)
            return receipt
        if status not in {"queued", "running"}:
            raise ValueError("Malformed Git operation status")
        time.sleep(POLL_SECONDS)
        kwargs = {id_field: identifier}
        if project is not None:
            kwargs["project_id"] = project
        receipt = sender(socket, op, **kwargs)
        if not isinstance(receipt, dict) or receipt.get("id") != identifier:
            raise ValueError("Incompatible Git operation status")


def _submit(sender, socket, op, kwargs, *, wait: bool, kind: str, project=None,
            request_key=None):
    key = request_key or secrets.token_hex(16)
    # This is printed BEFORE network send, allowing retry after lost ACK.
    print(f"dev: Git request key {key}", file=sys.stderr, flush=True)
    receipt = sender(socket, op, **kwargs, request_key=key, timeout=20)
    if not isinstance(receipt, dict):
        raise ValueError("Malformed Git operation submission")
    if wait:
        return _wait(sender, socket, receipt, kind, project)
    return receipt


def dispatch(args, sender):
    socket = args.socket
    if args.command == "project":
        if args.action == "import":
            return (_submit(sender, socket, "import_submit",
                            {"url": args.url, "slug": args.slug, "name": args.name},
                            wait=args.wait, kind="import",
                            request_key=args.request_key), "git_operation")
        if args.action == "import-status":
            return sender(socket, "import_status", id=args.id), "git_operation"
    if args.command != "repo":
        raise AssertionError("Not a Git operation")
    action = args.action
    project = args.project_id
    if action == "list":
        detail = sender(socket, "project_detail", project_id=project)
        return {"project_id": project, "repositories": detail["repositories"]}, "repo_list"
    if action == "status":
        return sender(socket, "repository_status", project_id=project,
                      repository_id=args.repository_id), "repo_status"
    if action == "update":
        return sender(socket, "repository_update", project_id=project,
                      repository_id=args.repository_id,
                      expected_head=args.expected_head), "repo_status"
    if action == "check":
        return sender(socket, "repository_check", project_id=project,
                      repository_id=args.repository_id), "repo_status"
    if action == "sync-policy":
        return sender(socket, "repository_sync_policy", project_id=project,
                      repository_id=args.repository_id, policy=args.mode), "repo_status"
    if action == "update-all":
        return (_submit(sender, socket, "repository_bulk_submit",
                        {"project_id": project}, wait=args.wait, kind="repo_bulk",
                        project=project, request_key=args.request_key), "git_operation")
    if action == "update-status":
        return sender(socket, "repository_bulk_status", project_id=project,
                      id=args.id), "git_operation"
    if action == "add":
        return (_submit(sender, socket, "repository_add_submit",
                        {"project_id": project, "url": args.url, "name": args.name,
                         "branch": args.branch},
                        wait=args.wait, kind="repo_add", project=project,
                        request_key=args.request_key), "git_operation")
    if action == "add-status":
        return sender(socket, "repository_add_status", project_id=project,
                      id=args.id), "git_operation"
    if action == "branch-preview":
        return sender(socket, "branch_preview", project_id=project,
                      branch=args.branch, repository_id=args.repository), "branch_preview"
    if action == "branch-apply":
        return sender(socket, "branch_apply", project_id=project,
                      branch=args.branch, repository_id=args.repository,
                      revision=args.revision), "branch_apply"
    raise AssertionError("Unexpected Git operation")


def _line(line, label, value):
    return line(label, value)


def render(name: str, payload: object, line) -> str:
    if not isinstance(payload, dict):
        raise ValueError("Malformed Git controller result")
    if name == "repo_list":
        entries = payload.get("repositories")
        if not isinstance(entries, list):
            raise ValueError("Malformed repository list")
        if not entries:
            return "No repositories registered."
        return "\n\n".join("\n".join([
            _line(line, "Repository", row["name"]),
            _line(line, "ID", row["id"]),
        ]) for row in entries)
    if name == "repo_status":
        return "\n".join([
            _line(line, label, payload.get(key))
            for key, label in (
                ("name", "Repository"), ("id", "ID"), ("branch", "Branch"),
                ("head", "HEAD"), ("upstream", "Upstream"), ("dirty", "Dirty"),
                ("ahead", "Ahead"), ("behind", "Behind"),
            )
        ])
    if name == "git_operation":
        lines = [
            _line(line, "Operation", payload.get("id")),
            _line(line, "State", payload.get("status")),
        ]
        for key, label in (
            ("project_id", "Project"), ("repo_id", "Repository"),
            ("error_code", "Error"), ("slug", "Slug"),
        ):
            if payload.get(key):
                lines.append(_line(line, label, payload[key]))
        if isinstance(payload.get("items"), list):
            for item in payload["items"]:
                lines.append("")
                lines.append(_line(line, "Repository", item.get("name")))
                lines.append(_line(line, "Result", item.get("status")))
                if item.get("reason"):
                    lines.append(_line(line, "Reason", item["reason"]))
        return "\n".join(lines)
    if name == "branch_preview":
        lines = [
            _line(line, "Branch", payload.get("requested_branch")),
            _line(line, "Ready", payload.get("ready")),
            _line(line, "Revision", payload.get("revision")),
        ]
        for item in payload.get("entries", []):
            lines += ["", _line(line, "Repository", item.get("name")),
                      _line(line, "Action", item.get("action")),
                      _line(line, "Target", item.get("target_branch")),
                      _line(line, "Fallback", item.get("fallback"))]
        return "\n".join(lines)
    if name == "branch_apply":
        lines = [_line(line, "Branch", payload.get("requested_branch",
                                                   payload.get("branch")))]
        for item in payload.get("results", payload.get("entries", [])):
            lines.extend(["", _line(line, "Repository", item.get("name")),
                          _line(line, "Action", item.get("action"))])
        return "\n".join(lines)
    raise ValueError("Unexpected Git display mode")
