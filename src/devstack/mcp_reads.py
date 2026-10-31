"""MCP is an independent client of the shared DevStack controller IPC.

The OAuth-protected public process uses the same exact-shape Unix protocol
as Web and CLI; it never imports controller internals or accesses Docker.
Project and resource context are validated by the existing controller.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from uuid import uuid4

from devstack.ipc.client import send, ControllerUnavailable, ControllerRejected

PROJECT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.I)
CONTAINER_ID = re.compile(r"[a-f0-9]{64}\Z")
MAX_PROJECTS = 100
MAX_REPOSITORIES = 100


class MCPReadError(ValueError):
    """Fixed errors: no private Web response body is disclosed on failure."""


def _uuid(value: str) -> str:
    if not isinstance(value, str) or not PROJECT_ID.fullmatch(value):
        raise MCPReadError("invalid_resource_id")
    return value.lower()


def _container(value: str) -> str:
    if not isinstance(value, str) or not CONTAINER_ID.fullmatch(value):
        raise MCPReadError("invalid_container_id")
    return value


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fields(value: dict, allowed: tuple[str, ...]) -> dict:
    return {field: value[field] for field in allowed if field in value}


def _operation_key(value: str | None) -> str:
    key = uuid4().hex if value is None else value
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key):
        raise MCPReadError("invalid_request_key")
    return key


def _revision(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise MCPReadError("invalid_revision")
    return value


def _short_text(value: str, label: str, limit: int = 2048) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MCPReadError("invalid_" + label)
    return value


class MCPReadClient:
    def __init__(self, socket_path: Path = Path("/run/devstack-control/control.sock"),
                 *, transport=None):
        self.socket_path = Path(socket_path)
        self.transport = transport

    def _call(self, operation: str, *, timeout: int = 30, **fields) -> dict:
        """Use the exact same versioned Unix IPC contract as Web and CLI."""
        if type(timeout) is not int or not 1 <= timeout <= 240:
            raise MCPReadError("invalid_timeout")
        try:
            result = (self.transport(operation, **fields) if self.transport is not None
                      else send(self.socket_path, operation, timeout=timeout, **fields))
            if not isinstance(result, dict):
                raise MCPReadError("invalid_controller_response")
            return result
        except (ControllerUnavailable, ControllerRejected) as exc:
            raise MCPReadError("controller_unavailable" if isinstance(exc, ControllerUnavailable)
                               else "controller_rejected_" + exc.code[:60]) from exc

    def probe(self) -> str:
        try:
            result = self._call("health", timeout=2)
            return "connected" if result.get("alive") is True else "unavailable"
        except MCPReadError as exc:
            return ("recovery_required" if str(exc).startswith("controller_rejected_")
                    else "unavailable")

    @staticmethod
    def _global(source: str, data: dict) -> dict:
        return {"scope": "devstack", "source": {"type": source},
                "captured_at": _stamp(), **data}

    def projects(self) -> dict:
        values = self._call("projects").get("projects")
        if not isinstance(values, list) or len(values) > MAX_PROJECTS:
            raise MCPReadError("invalid_projects")
        entries = []
        for value in values:
            if not isinstance(value, dict) or not PROJECT_ID.fullmatch(str(value.get("id", ""))):
                raise MCPReadError("invalid_projects")
            entries.append(_fields(value, ("id", "name", "slug")))
        return self._global("project_registry", {"projects": entries, "truncated": False})

    def runtime(self) -> dict:
        value = self._call("status")
        project = None
        if value.get("project_id") is not None:
            project = self._project_identity(value["project_id"])
        return self._global("runtime", {"runtime": _fields(value, (
            "phase", "project_id", "updated_ns")), "active_project": project})

    def _project_identity(self, project_id: str) -> dict:
        target = _uuid(project_id)
        for project in self.projects()["projects"]:
            if project["id"] == target:
                return {"id": project["id"], "name": project["name"]}
        raise MCPReadError("project_not_found")

    def _project(self, project_id: str, source_type: str, source_id: str | None = None,
                 source_name: str | None = None) -> dict:
        project = self._project_identity(project_id)
        source = {"type": source_type}
        if source_id is not None:
            source["id"] = source_id
        if source_name is not None:
            source["name"] = source_name
        return {"scope": "project", "project": project,
                "source": source, "captured_at": _stamp()}

    def project(self, project_id: str) -> dict:
        target = _uuid(project_id)
        data = self._call("project_detail", project_id=target)
        entry = data.get("project", {})
        if not isinstance(entry, dict) or entry.get("id") != target:
            raise MCPReadError("project_identity_mismatch")
        identity = {"id": target, "name": entry.get("name")}
        repositories = data.get("repositories", [])
        if not isinstance(repositories, list) or len(repositories) > MAX_REPOSITORIES:
            raise MCPReadError("invalid_repositories")
        result = self._project(target, "project_registry")
        if result["project"] != identity:
            raise MCPReadError("project_identity_mismatch")
        return {**result,
                "repositories": [_fields(row, ("id", "name")) for row in repositories],
                "applications": [_fields(row, ("name", "url", "service", "port"))
                                 for row in data.get("applications", [])[:100]],
                "tasks": [_fields(row, ("name", "service", "mode"))
                          for row in data.get("tasks", [])[:100]],
                "truncated": False}

    def repository_status(self, project_id: str, repository_id: str) -> dict:
        target, repo = _uuid(project_id), _uuid(repository_id)
        detail = self.project(target)
        if not any(row.get("id") == repo for row in detail["repositories"]):
            raise MCPReadError("repository_not_in_project")
        name = next(row["name"] for row in detail["repositories"] if row["id"] == repo)
        data = self._call("repository_status", project_id=target, repository_id=repo, timeout=30)
        status = _fields(data, ("branch", "head", "upstream", "ahead", "behind",
                                "dirty", "status", "working_tree", "staged", "unstaged",
                                "untracked", "detached"))
        sync = data.get("sync", {})
        if isinstance(sync, dict):
            status["sync"] = _fields(sync, ("policy", "status", "error_code", "last_checked_ns",
                                            "last_updated_ns", "ahead", "behind", "head"))
        return {**self._project(target, "repository", repo, name),
                "repository_status": status}

    def services(self, project_id: str) -> dict:
        target = _uuid(project_id)
        data = self._call("services", project_id=target, timeout=60)
        if data.get("project_id") != target:
            raise MCPReadError("project_identity_mismatch")
        services = data.get("services", [])
        if not isinstance(services, list) or len(services) > 96:
            raise MCPReadError("invalid_services")
        return {**self._project(target, "services"),
                "active": data.get("active") is True,
                "services": services}

    def service_logs(self, project_id: str, container_id: str, tail: int = 100) -> dict:
        target, container = _uuid(project_id), _container(container_id)
        if type(tail) is not int or not 1 <= tail <= 200:
            raise MCPReadError("invalid_tail")
        # Existing controller validates active project, activation generation,
        # project-owned Compose labels, and exact container identity.
        data = self._call("service_logs", project_id=target, container_id=container, tail=tail, timeout=30)
        if data.get("container_id") != container or not isinstance(data.get("service"), str):
            raise MCPReadError("container_identity_mismatch")
        return {**self._project(target, "service_logs", container, data["service"]),
                "requested_tail": tail,
                "log": _fields(data, ("text", "logs", "truncated", "lines", "output"))}

    def jobs(self, project_id: str) -> dict:
        target = _uuid(project_id)
        data = self._call("jobs", project_id=target)
        jobs = data.get("jobs")
        if not isinstance(jobs, list) or len(jobs) > 40:
            raise MCPReadError("invalid_jobs")
        if any(not isinstance(row, dict) or row.get("project_id") != target for row in jobs):
            raise MCPReadError("job_identity_mismatch")
        return {**self._project(target, "jobs"),
                "jobs": [_fields(row, ("id", "kind", "status", "action",
                                       "started_ns", "finished_ns", "exit_code"))
                         for row in jobs]}

    def job_logs(self, project_id: str, job_id: str, offset: int = 0) -> dict:
        target, job = _uuid(project_id), _uuid(job_id)
        if type(offset) is not int or not 0 <= offset <= 2 * 1024 * 1024:
            raise MCPReadError("invalid_offset")
        # Validate ownership even for empty logs. The existing job endpoint
        # rejects IDs belonging to another project.
        detail = self._call("job", project_id=target, id=job)
        if detail.get("project_id") != target or detail.get("id") != job:
            raise MCPReadError("job_identity_mismatch")
        data = self._call("job_logs", project_id=target, id=job, offset=offset)
        return {**self._project(target, "job_logs", job),
                "status": detail.get("status"), "offset": data.get("offset"),
                "next_offset": data.get("next_offset"), "complete": data.get("complete"),
                "text": data.get("text", "")}

    def diagnostics(self, minutes: int = 5) -> dict:
        if type(minutes) is not int or minutes not in (1, 5, 30):
            raise MCPReadError("invalid_window")
        data = self._call("diagnostics_snapshot", minutes=minutes)
        events = data.get("events", [])
        if not isinstance(events, list):
            raise MCPReadError("invalid_diagnostics")
        sources = data.get("sources", {})
        coverage = {name: sources.get(name, "unavailable")
                    for name in ("controller", "runtime", "web", "updater", "mcp")}
        storage = data.get("web_storage")
        if not isinstance(storage, dict):
            storage = {"mode": "unavailable", "error_code": "storage_unavailable"}
        return self._global("diagnostics", {"minutes": minutes,
            "sources": coverage, "events": events[-120:],
            "truncated": bool(data.get("truncated", False)) or len(events) > 120,
            "partial": any(value != "available" for value in coverage.values()),
            "web_storage": storage,
            "updater_status": data.get("updater_status"),
            "support_captured_at": data.get("support_captured_at"),
            "network_preflight": data.get("network_preflight")})


    def transition(self, action: str, project_id: str | None = None,
                   request_key: str | None = None) -> dict:
        """Submit the same durable, idempotent transition as the Web/CLI."""
        if action not in ("activate", "switch", "deactivate", "reconcile"):
            raise MCPReadError("invalid_transition")
        if action in ("activate", "switch"):
            if project_id is None:
                raise MCPReadError("project_required")
            project_id = _uuid(project_id)
            context = self._project(project_id, "runtime_transition")
        elif project_id is not None:
            raise MCPReadError("unexpected_project")
        else:
            context = self._global("runtime_transition", {})
        key = uuid4().hex if request_key is None else request_key
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key):
            raise MCPReadError("invalid_request_key")
        receipt = self._call("transition_submit", action=action,
                             project_id=project_id, request_key=key, timeout=30)
        if receipt.get("action") != action or receipt.get("project_id") != project_id:
            raise MCPReadError("transition_identity_mismatch")
        return {**context, "operation": _fields(receipt, (
            "id", "action", "project_id", "status", "error_code",
            "started_ns", "finished_ns", "result"))}

    def transition_status(self, operation_id: str) -> dict:
        key = _uuid(operation_id)
        data = self._call("transition_status", id=key)
        if data.get("id") != key:
            raise MCPReadError("transition_identity_mismatch")
        target = data.get("project_id")
        context = (self._project(_uuid(target), "runtime_transition", key)
                   if target is not None else self._global("runtime_transition", {}))
        return {**context, "operation": _fields(data, (
            "id", "action", "project_id", "status", "error_code",
            "started_ns", "finished_ns", "result"))}

    def repository_check(self, project_id: str, repository_id: str) -> dict:
        target, repo = _uuid(project_id), _uuid(repository_id)
        before = self.project(target)
        name = next((x["name"] for x in before["repositories"]
                     if x.get("id") == repo), None)
        if name is None:
            raise MCPReadError("repository_not_in_project")
        data = self._call("repository_check", project_id=target,
                          repository_id=repo, timeout=180)
        return {**self._project(target, "repository", repo, name),
                "repository_status": _fields(data, (
                    "branch", "head", "upstream", "ahead", "behind", "dirty")),
                "sync": _fields(data.get("sync", {}), (
                    "policy", "status", "error_code", "last_checked_ns",
                    "last_updated_ns", "ahead", "behind", "head"))}

    def repository_update(self, project_id: str, repository_id: str,
                          expected_head: str) -> dict:
        target, repo = _uuid(project_id), _uuid(repository_id)
        if not isinstance(expected_head, str) or not re.fullmatch(r"[a-f0-9]{40,64}", expected_head):
            raise MCPReadError("invalid_expected_head")
        before = self.project(target)
        name = next((x["name"] for x in before["repositories"]
                     if x.get("id") == repo), None)
        if name is None:
            raise MCPReadError("repository_not_in_project")
        # Controller enforces clean worktree, matching HEAD, coordinator lock,
        # active-project reconciliation and configured service reactions.
        data = self._call("repository_update", project_id=target,
                          repository_id=repo, expected_head=expected_head, timeout=180)
        return {**self._project(target, "repository", repo, name),
                "previous_head": expected_head,
                "repository_status": _fields(data, (
                    "branch", "head", "upstream", "ahead", "behind", "dirty")),
                "sync": _fields(data.get("sync", {}), (
                    "policy", "status", "error_code", "last_checked_ns",
                    "last_updated_ns", "ahead", "behind", "head")),
                "note": "Configured service reactions may run after this update."}

    def service_action(self, project_id: str, service: str, action: str) -> dict:
        target = _uuid(project_id)
        if action not in ("restart", "redeploy"):
            raise MCPReadError("invalid_service_action")
        if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,90}", service):
            raise MCPReadError("invalid_service")
        # Controller verifies active generation, configured Compose service
        # and maintenance admission; it owns the resulting asynchronous job.
        result = self._call("compose_service", project_id=target,
                            service=service, action=action, timeout=30)
        if not isinstance(result.get("job_id"), str):
            raise MCPReadError("invalid_job_receipt")
        return {**self._project(target, "service", service, service),
                "action": action, "job_id": _uuid(result["job_id"]), "status": "submitted"}

    def run_task(self, project_id: str, task_name: str) -> dict:
        target = _uuid(project_id)
        if not isinstance(task_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,90}", task_name):
            raise MCPReadError("invalid_task_name")
        # Resolve the existing task name before execution; no arbitrary command.
        detail = self.project(target)
        if not any(row.get("name") == task_name for row in detail["tasks"]):
            raise MCPReadError("task_not_in_project")
        result = self._call("task", project_id=target, name=task_name, timeout=30)
        return {**self._project(target, "task", source_name=task_name),
                "job_id": _uuid(result["job_id"]), "status": "submitted"}

    def job_detail(self, project_id: str, job_id: str) -> dict:
        target, job = _uuid(project_id), _uuid(job_id)
        data = self._call("job", project_id=target, id=job)
        if data.get("id") != job or data.get("project_id") != target:
            raise MCPReadError("job_identity_mismatch")
        return {**self._project(target, "job", job),
                "job": _fields(data, ("id", "kind", "status", "action", "cancel_policy",
                                       "started_ns", "finished_ns", "exit_code"))}

    def cancel_job(self, project_id: str, job_id: str) -> dict:
        target, job = _uuid(project_id), _uuid(job_id)
        data = self._call("job_cancel", project_id=target, id=job, timeout=60)
        if data.get("id") != job or data.get("project_id") != target:
            raise MCPReadError("job_identity_mismatch")
        return {**self._project(target, "job", job),
                "job": _fields(data, ("id", "status", "cancel_policy",
                                       "started_ns", "finished_ns", "exit_code"))}

    # v0.7.8: these methods are thin clients of existing controller IPC
    # operations. The controller remains the sole execution/validation authority.

    def _repo_identity(self, project_id: str, repository_id: str) -> tuple[str, str, str]:
        target, repo = _uuid(project_id), _uuid(repository_id)
        detail = self.project(target)
        name = next((item["name"] for item in detail["repositories"]
                     if item["id"] == repo), None)
        if name is None:
            raise MCPReadError("repository_not_in_project")
        return target, repo, name

    def repository_sync(self, project_id: str, repository_id: str) -> dict:
        target, repo, name = self._repo_identity(project_id, repository_id)
        result = self._call("repository_sync_state", project_id=target, repository_id=repo)
        return {**self._project(target, "repository_sync", repo, name), "sync": result}

    def repository_sync_policy(self, project_id: str, repository_id: str,
                               policy: str) -> dict:
        if policy not in ("manual", "check", "update"):
            raise MCPReadError("invalid_sync_policy")
        target, repo, name = self._repo_identity(project_id, repository_id)
        result = self._call("repository_sync_policy", project_id=target,
                            repository_id=repo, policy=policy)
        return {**self._project(target, "repository_sync", repo, name), "sync": result}

    def repository_reactions(self, project_id: str, repository_id: str) -> dict:
        target, repo, name = self._repo_identity(project_id, repository_id)
        result = self._call("repository_reactions", project_id=target, repository_id=repo)
        return {**self._project(target, "repository_reactions", repo, name),
                "services": result.get("services", []),
                "events": result.get("events", [])}

    def repository_reaction_policy(self, project_id: str, repository_id: str,
                                   service: str, action: str) -> dict:
        if action not in ("none", "restart", "redeploy"):
            raise MCPReadError("invalid_reaction_action")
        target, repo, name = self._repo_identity(project_id, repository_id)
        result = self._call("repository_reaction_policy", project_id=target,
                            repository_id=repo, service=_short_text(service, "service", 90),
                            action=action)
        return {**self._project(target, "repository_reactions", repo, name),
                "services": result.get("services", []),
                "events": result.get("events", [])}

    def repository_bulk_update(self, project_id: str,
                               request_key: str | None = None) -> dict:
        target = _uuid(project_id)
        context = self._project(target, "repository_bulk_update")
        result = self._call("repository_bulk_submit", project_id=target,
                            request_key=_operation_key(request_key))
        if result.get("project_id") != target:
            raise MCPReadError("project_identity_mismatch")
        return {**context, "operation": result}

    def repository_bulk_status(self, project_id: str, operation_id: str) -> dict:
        target, operation = _uuid(project_id), _uuid(operation_id)
        result = self._call("repository_bulk_status", project_id=target, id=operation)
        if result.get("project_id") != target or result.get("id") != operation:
            raise MCPReadError("operation_identity_mismatch")
        return {**self._project(target, "repository_bulk_update", operation),
                "operation": result}

    def repository_add(self, project_id: str, url: str, name: str,
                       branch: str = "", request_key: str | None = None) -> dict:
        target = _uuid(project_id)
        context = self._project(target, "repository_add")
        result = self._call("repository_add_submit", project_id=target,
                            url=_short_text(url, "url"),
                            name=_short_text(name, "repository_name", 120),
                            branch=branch, request_key=_operation_key(request_key))
        if result.get("project_id") != target:
            raise MCPReadError("project_identity_mismatch")
        return {**context, "operation": result}

    def repository_add_status(self, project_id: str, operation_id: str) -> dict:
        target, operation = _uuid(project_id), _uuid(operation_id)
        result = self._call("repository_add_status", project_id=target, id=operation)
        if result.get("project_id") != target or result.get("id") != operation:
            raise MCPReadError("operation_identity_mismatch")
        return {**self._project(target, "repository_add", operation),
                "operation": result}

    def branch_preview(self, project_id: str, branch: str,
                       repository_id: str | None = None) -> dict:
        target = _uuid(project_id)
        repo = self._repo_identity(target, repository_id)[1] if repository_id else None
        result = self._call("branch_preview", project_id=target,
                            branch=_short_text(branch, "branch", 200), repository_id=repo,
                            timeout=180)
        if result.get("project_id") != target or result.get("repository_id") != repo:
            raise MCPReadError("branch_identity_mismatch")
        return {**self._project(target, "branch_preview", repo), "preview": result}

    def branch_apply(self, project_id: str, branch: str, revision: str,
                     repository_id: str | None = None) -> dict:
        target = _uuid(project_id)
        repo = self._repo_identity(target, repository_id)[1] if repository_id else None
        result = self._call("branch_apply", project_id=target,
                            branch=_short_text(branch, "branch", 200),
                            repository_id=repo, revision=_revision(revision), timeout=180)
        if result.get("project_id") != target:
            raise MCPReadError("branch_identity_mismatch")
        return {**self._project(target, "branch", repo), "result": result}

    def create_project(self, slug: str, name: str) -> dict:
        result = self._call("project_create", slug=_short_text(slug, "slug", 100),
                            name=_short_text(name, "name", 150))
        identifier = _uuid(result.get("id"))
        registered = self._project_identity(identifier)
        if registered["name"] != result.get("name"):
            raise MCPReadError("project_identity_mismatch")
        return {**self._project(identifier, "project_registry"), "created": True}

    def import_project(self, url: str, slug: str, name: str,
                       request_key: str | None = None) -> dict:
        result = self._call("import_submit", url=_short_text(url, "url"),
                            slug=_short_text(slug, "slug", 100),
                            name=_short_text(name, "name", 150),
                            request_key=_operation_key(request_key))
        if not isinstance(result.get("id"), str):
            raise MCPReadError("invalid_import_receipt")
        return self._global("project_import", {"operation": result})

    def import_status(self, operation_id: str) -> dict:
        operation = _uuid(operation_id)
        result = self._call("import_status", id=operation)
        if result.get("id") != operation:
            raise MCPReadError("operation_identity_mismatch")
        if result.get("project_id"):
            self._project_identity(_uuid(result["project_id"]))
        return self._global("project_import", {"operation": result})

    def rename_project(self, project_id: str, name: str) -> dict:
        target = _uuid(project_id)
        self._project_identity(target)
        result = self._call("project_rename", project_id=target,
                            name=_short_text(name, "name", 150))
        if result.get("id") != target:
            raise MCPReadError("project_identity_mismatch")
        return {**self._project(target, "project_registry"),
                "project_details": _fields(result, ("id", "slug", "name"))}

    def compose_config(self, project_id: str) -> dict:
        target = _uuid(project_id)
        data = self._call("compose_read", project_id=target)
        if not isinstance(data.get("content"), str) or not isinstance(data.get("revision"), str):
            raise MCPReadError("invalid_compose_config")
        return {**self._project(target, "compose_config"), "config": data}

    def compose_save(self, project_id: str, content: str, revision: str) -> dict:
        target = _uuid(project_id)
        if not isinstance(content, str) or not content or len(content.encode("utf-8")) > 48 * 1024:
            raise MCPReadError("invalid_compose_content")
        if revision != "none":
            _revision(revision)
        result = self._call("compose_save", project_id=target, content=content,
                            revision=revision, timeout=180)
        return {**self._project(target, "compose_config"), "result": result}

    def project_settings(self, project_id: str) -> dict:
        target = _uuid(project_id)
        data = self._call("settings_review", project_id=target)
        if not isinstance(data.get("revision"), str):
            raise MCPReadError("invalid_settings_revision")
        return {**self._project(target, "project_settings"), "settings": data}

    def settings_save(self, project_id: str, revision: str,
                      applications: list[dict], tasks: list[dict]) -> dict:
        target = _uuid(project_id)
        _revision(revision)
        if (not isinstance(applications, list) or not isinstance(tasks, list)
                or len(applications) > 30 or len(tasks) > 40):
            raise MCPReadError("invalid_settings")
        result = self._call("settings_replace_checked", project_id=target,
                            revision=revision, applications=applications, tasks=tasks,
                            timeout=180)
        return {**self._project(target, "project_settings"), "result": result}

    def source_review(self, project_id: str) -> dict:
        target = _uuid(project_id)
        result = self._call("source_review", project_id=target)
        return {**self._project(target, "source_review"), "review": result}

    def source_apply(self, project_id: str, revision: str, selected: list,
                     overwrites: list) -> dict:
        target = _uuid(project_id)
        if not isinstance(selected, list) or not isinstance(overwrites, list):
            raise MCPReadError("invalid_source_selection")
        result = self._call("source_apply", project_id=target,
                            revision=_revision(revision),
                            selected=selected, overwrites=overwrites, timeout=180)
        return {**self._project(target, "source_review"), "result": result}

    def webhooks(self, project_id: str) -> dict:
        target = _uuid(project_id)
        result = self._call("webhooks", project_id=target)
        return {**self._project(target, "webhooks"), "webhooks": result.get("webhooks", [])}

    def webhook_upsert(self, project_id: str, route: str, service: str,
                       port: int, destination: str) -> dict:
        target = _uuid(project_id)
        result = self._call("webhook_upsert", project_id=target,
                            route=_short_text(route, "route", 63),
                            service=_short_text(service, "service", 90),
                            port=port, destination=_short_text(destination, "destination", 1024))
        if result.get("route") != route:
            raise MCPReadError("webhook_identity_mismatch")
        return {**self._project(target, "webhook", source_name=route), "webhook": result}

    def webhook_remove(self, project_id: str, route: str) -> dict:
        target = _uuid(project_id)
        route = _short_text(route, "route", 63)
        result = self._call("webhook_remove", project_id=target, route=route)
        if result.get("removed") is not True:
            raise MCPReadError("invalid_webhook_receipt")
        return {**self._project(target, "webhook", source_name=route), "removed": True}

    def compose_action(self, project_id: str, action: str) -> dict:
        target = _uuid(project_id)
        if action not in ("build", "restart"):
            raise MCPReadError("invalid_compose_action")
        result = self._call("compose", project_id=target, action=action)
        return {**self._project(target, "compose_job"), "action": action,
                "job_id": _uuid(result.get("job_id")), "status": "submitted"}

    def adhoc(self, project_id: str, kind: str, service: str, image: str,
              network: str, command: list[str]) -> dict:
        target = _uuid(project_id)
        if (kind not in ("run", "image") or network not in ("none", "project")
                or not isinstance(command, list) or not 1 <= len(command) <= 64
                or any(not isinstance(arg, str) or not arg or len(arg) > 2048
                       for arg in command)):
            raise MCPReadError("invalid_adhoc_command")
        result = self._call("adhoc", project_id=target, kind=kind,
                            service=service, image=image, network=network,
                            command=command)
        return {**self._project(target, "adhoc_job"), "job_id": _uuid(result.get("job_id")),
                "status": "submitted"}

    def private_ports(self, project_id: str) -> dict:
        target = _uuid(project_id)
        data = self._call("private_ports", project_id=target)
        return {**self._project(target, "private_ports"), "ports": data}
