"""Versioned, exact-field controller IPC contract shared by all local clients.

Only the privileged controller executes these operations. This module is safe
to import from unprivileged HTTP/SSH processes: no Docker or database imports.
"""
from __future__ import annotations

MAX_MESSAGE = 64 * 1024
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 1

# Request shapes are intentionally exact, not permissive. Optional values
# still occupy their named fields (typically as JSON null).
_OPERATION_FIELDS = {
    "protocol_info": (),
    "health": (),
    "status": (),
    "transition_submit": ("action", "project_id", "request_key"),
    "transition_status": ("id",),
    "transition_list": (),
    "diagnostics_snapshot": ("minutes",),
    "projects": (),
    "project_detail": ("project_id",),
    "project_rename": ("project_id", "name"),
    "source_review": ("project_id",),
    "source_apply": ("project_id", "revision", "selected", "overwrites"),
    "repository_status": ("project_id", "repository_id"),
    "repository_update": ("project_id", "repository_id", "expected_head"),
    "repository_bulk_submit": ("project_id", "request_key"),
    "repository_bulk_status": ("project_id", "id"),
    "branch_preview": ("project_id", "branch", "repository_id"),
    "branch_apply": ("project_id", "branch", "repository_id", "revision"),
    "repository_add_submit": ("project_id", "url", "name", "branch", "request_key"),
    "repository_add_status": ("project_id", "id"),
    "github_ssh_status": (),
    "github_ssh_generate": (),
    "github_ssh_test": (),
    "compose_read": ("project_id",),
    "compose_save": ("project_id", "content", "revision"),
    "settings_read": ("project_id",),
    "settings_replace": ("project_id", "applications", "tasks"),
    "webhooks": ("project_id",),
    "webhook_upsert": ("project_id", "route", "service", "port", "destination"),
    "webhook_remove": ("project_id", "route"),
    "project_create": ("slug", "name"),
    "import_submit": ("url", "slug", "name", "request_key"),
    "import_status": ("id",),
    "activate": ("project_id",),
    "switch": ("project_id",),
    "deactivate": (),
    "reconcile": (),
    "task": ("project_id", "name"),
    "compose": ("project_id", "action"),
    "job": ("project_id", "id"),
    "adhoc": ("project_id", "kind", "service", "image", "network", "command"),
    "jobs": ("project_id",),
    "services": ("project_id",),
    "private_ports": ("project_id",),
    "service_logs": ("project_id", "container_id", "tail"),
    "job_logs": ("project_id", "id", "offset"),
    "job_cancel": ("project_id", "id"),
}

# Immutable to callers; server and clients can never mutate the contract.
from types import MappingProxyType

OPERATIONS = MappingProxyType({
    name: frozenset(("op", *fields))
    for name, fields in _OPERATION_FIELDS.items()
})


class ProtocolError(ValueError):
    """Malformed or unrecognized local controller request."""


def validate_request(request: object) -> str:
    """Validate the exact allowed top-level shape. Domain checks stay in control."""
    if not isinstance(request, dict) or not isinstance(request.get("op"), str):
        raise ProtocolError("Expected a controller operation")
    operation = request["op"]
    if operation not in OPERATIONS or set(request) != OPERATIONS[operation]:
        raise ProtocolError("Unexpected or missing controller parameters")
    return operation


def protocol_info() -> dict:
    return {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR}
