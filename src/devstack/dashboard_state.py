"""Framework-independent, read-only dashboard view model for the v0.5 API.

The UI (today Jinja, in v0.6 a new frontend) consumes a stable JSON shape.
Never leak executable task arguments or project secret configuration through
this overview. Every section fails independently so the dashboard remains useful
when Docker/Tailscale are offline.
"""
from __future__ import annotations

from .workspace import Project
from .core import OperationError, repo_branch, repo_dirty
from .applications import ApplicationError, load_applications, compose_states, application_status
from .command_runner import CommandError, load_tasks, recent_jobs
from .docker_stacks import DockerError, current_job
from .container_inventory import compose_containers
from .dev_ports import published_ports
from .webhooks import WebhookError, load_webhooks
from . import runtime


def _runtime() -> dict:
    try:
        result = runtime.status()
        return {key: value for key, value in result.items()
                if key not in ("token", "boot_id")}
    except (runtime.RuntimeControlError, OSError, ValueError):
        return {"phase": "error", "message": "Runtime state unavailable"}


def _attempt(section: str, fn, errors: dict, default):
    try:
        return fn()
    except (ApplicationError, CommandError, DockerError, WebhookError,
            OperationError, runtime.RuntimeControlError, OSError, ValueError) as exc:
        # Errors are private to the authenticated/tailnet-only management API.
        # Do not allow the whole dashboard to disappear due to one subsystem.
        errors[section] = str(exc)
        return default


def overview(project: Project | None) -> dict:
    """Snapshot for a single workspace; no Flask, HTML, or browser assumptions."""
    result = {
        "schema_version": 1,
        "runtime": _runtime(),
        "project": None,
        "repositories": [],
        "applications": [],
        "services": [],
        "containers": [],
        "ports": [],
        "tasks": [],
        "recent_jobs": [],
        "compose_job": None,
        "webhooks": [],
        "errors": {},
    }
    if project is None:
        return result

    name = project.name
    result["project"] = {"name": name}

    def repositories():
        records = []
        for repo in project.repos:
            try:
                records.append({"name": repo.name, "branch": repo_branch(repo),
                                "dirty": repo_dirty(repo), "error": None})
            except OperationError:
                records.append({"name": repo.name, "branch": None,
                                "dirty": None, "error": "Git repository unavailable"})
        return records

    errors = result["errors"]
    result["repositories"] = _attempt("repositories", repositories, errors, [])

    applications = _attempt("applications", lambda: load_applications(name), errors, ())
    states = {}
    if result["runtime"].get("phase") == "active":
        states = _attempt("services", lambda: compose_states(name), errors, {})
        containers = _attempt("containers", lambda: compose_containers(name), errors, ())
        result["containers"] = list(containers)
        result["services"] = [
            {"name": key, "state": value}
            for key, value in sorted((states or {}).items())
        ]
    result["applications"] = [
        {"name": app.name, "service": app.service, "container_port": app.port,
         "state": application_status(states, app) if states else "inactive"}
        for app in applications
    ]

    if result["runtime"].get("phase") == "active":
        mappings = _attempt("ports", lambda: published_ports(name), errors, ())
        result["ports"] = [
            {"service": p.service, "container_port": p.container_port,
             "host_port": p.host_port, "url": p.url}
            for p in mappings
        ]

    tasks = _attempt("tasks", lambda: load_tasks(name), errors, ())
    result["tasks"] = [
        {"name": task.name, "mode": task.mode, "service": task.service,
         "image": task.image, "network": task.network}
        for task in tasks
    ]

    jobs = _attempt("recent_jobs", lambda: recent_jobs(name), errors, [])
    result["recent_jobs"] = [
        {"id": job.get("id"), "state": job.get("state"),
         "service": job.get("service"), "mode": job.get("mode"),
         "started": job.get("started")}
        for job in jobs[:10]
    ]

    compose = _attempt("compose_job", lambda: current_job(name), errors, None)
    if compose is not None:
        result["compose_job"] = {
            "id": compose.get("id"), "action": compose.get("action"),
            "state": compose.get("state"),
        }

    webhooks = _attempt("webhooks", lambda: load_webhooks(name), errors, ())
    result["webhooks"] = [
        {"name": hook.name, "application": hook.application, "path": hook.path,
         "enabled": result["runtime"].get("phase") == "active"}
        for hook in webhooks
    ]
    return result
