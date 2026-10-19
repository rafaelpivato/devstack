"""DevStack v2: project-scoped owner dashboard and HTTP API.

This process owns neither Docker nor the SQLite registry. Every operation
goes over the controller's restricted Unix socket; selecting a project in
one browser tab does not mutate the global active runtime.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import secrets

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request,
    session, url_for,
)
from werkzeug.exceptions import HTTPException

from .client import ControllerRejected, ControllerUnavailable, send

SAFE_READ = {"GET", "HEAD", "OPTIONS"}
SAMPLE_URL = "https://github.com/rafaelpivato/devstack/tree/main/examples/smoke"
NOT_FOUND = {"WebhookNotFound", "ProjectNotFound", "ImportNotFound", "RepositoryNotFound"}
INVALID = {"WebhookInvalid", "ConfigurationInvalid", "DomainError", "ProtocolError", "GitHubSourceError",
           "ManifestError", "ProjectProvisionError", "WorkspaceError", "RepositoryGitInvalid", "SourceReviewInvalid"}
CONFLICT = {"WebhookBusy", "ConfigurationConflict", "ControlError", "ProjectConflict", "JobError", "DockerControlError", "ImportConflict", "RepositoryGitError", "SourceSyncConflict"}


def create_app(config: dict | None = None, *, transport=None) -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(
        SECRET_KEY=os.environ.get("DEVSTACK_SECRET_KEY") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.environ.get("DEVSTACK_SECURE_COOKIES", "1") == "1",
        MAX_CONTENT_LENGTH=64 * 1024,
        CONTROLLER_SOCKET=os.environ.get(
            "DEVSTACK_CONTROL_SOCKET", "/run/devstack-control/control.sock"),
    )
    if config:
        app.config.update(config)

    def call(op: str, **fields):
        if transport is not None:
            return transport(op, **fields)
        return send(Path(app.config["CONTROLLER_SOCKET"]), op, **fields)

    def csrf() -> str:
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(32)
        return session["_csrf"]

    @app.before_request
    def guard_writes():
        if request.method in SAFE_READ:
            return
        supplied = request.headers.get("X-CSRF-Token")
        if not supplied:
            supplied = request.form.get("_csrf", "")
        expected = session.get("_csrf")
        if (not expected or not supplied
                or not secrets.compare_digest(expected, supplied)):
            abort(403)
        if request.is_json and request.content_length and request.content_length > 60 * 1024:
            abort(413)

    def problem(exc):
        if isinstance(exc, ControllerUnavailable):
            return 503, "DevStack controller is unavailable. Try again shortly."
        if isinstance(exc, ControllerRejected):
            if exc.code in NOT_FOUND:
                return 404, "The requested project does not exist."
            if exc.code in INVALID:
                return 422, "Invalid project configuration or request."
            if exc.code in CONFLICT:
                return 409, "The requested operation is not currently permitted."
        return 503, "The controller could not complete the operation."

    def json_error(exc):
        status, message = problem(exc)
        return jsonify({"ok": False, "message": message}), status

    def project_data(identifier):
        detail = call("project_detail", project_id=identifier)
        return detail

    def page(identifier=None):
        overview = call("projects")
        runtime = call("status")
        details = project_data(identifier) if identifier else None
        selected_id = details["project"]["id"] if details else None
        compose = call("compose_read", project_id=selected_id) if selected_id else None
        definitions = call("settings_read", project_id=selected_id) if selected_id else None
        webhooks = call("webhooks", project_id=selected_id)["webhooks"] if selected_id else ()
        return render_template(
            "v2_dashboard.html", projects=overview["projects"], runtime=runtime,
            detail=details, selected_id=selected_id, csrf=csrf(),
            compose=compose, definitions=definitions, webhooks=webhooks,
            example=request.args.get("example") == "smoke",
            sample_url=SAMPLE_URL, import_key=secrets.token_hex(16),
        )

    @app.get("/healthz")
    def healthz():
        try:
            result = call("health")
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)
        if result.get("runtime") == "error":
            return jsonify({"ok": False, "controller": "recovery_required"}), 503
        return jsonify({"ok": True, "controller": "connected"})

    @app.get("/")
    def index():
        try:
            return page()
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/projects/<identifier>")
    def selected_project(identifier):
        try:
            return page(identifier)
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    def redirect_to(identifier=None):
        return redirect(
            url_for("selected_project", identifier=identifier) if identifier
            else url_for("index"), code=303,
        )

    def perform(op, *, target=None, **fields):
        try:
            call(op, **fields)
            flash("Operation completed.", "success")
        except (ControllerUnavailable, ControllerRejected) as exc:
            flash(problem(exc)[1], "error")
        return redirect_to(target)

    def field(name):
        value = request.form.get(name, "")
        if not isinstance(value, str) or len(value) > 2048:
            abort(400)
        return value.strip()

    @app.post("/projects")
    def create_project():
        try:
            created = call("project_create", slug=field("slug"), name=field("name"))
            return redirect_to(created["id"])
        except (ControllerUnavailable, ControllerRejected) as exc:
            flash(problem(exc)[1], "error")
            return redirect_to()

    @app.post("/projects/import")
    def import_project():
        try:
            created = call(
                "import_submit", url=field("url"),
                slug=field("slug"), name=field("name"),
                request_key=field("request_key"),
            )
            return redirect(url_for("import_page", identifier=created["id"]), code=303)
        except (ControllerUnavailable, ControllerRejected) as exc:
            flash(problem(exc)[1], "error")
            return redirect_to()

    @app.get("/imports/<identifier>")
    def import_page(identifier):
        try:
            job = call("import_status", id=identifier)
            return render_template("v2_import.html", job=job)
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/projects/<identifier>/source-review")
    def project_source_review(identifier):
        try:
            review = call("source_review", project_id=identifier)
            runtime = call("status")
            return render_template("v2_source_review.html", review=review,
                                   runtime=runtime, csrf=csrf())
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/projects/<identifier>/source-review/apply")
    def project_source_apply_form(identifier):
        revision = field("revision")
        selected = request.form.getlist("selected")
        overwrites = request.form.getlist("overwrites")
        try:
            call("source_apply", project_id=identifier,
                 revision=revision, selected=selected, overwrites=overwrites)
            flash("Selected Git template changes applied.", "success")
        except (ControllerUnavailable, ControllerRejected) as exc:
            flash(problem(exc)[1], "error")
        return redirect(url_for("project_source_review", identifier=identifier), code=303)

    @app.get("/projects/<identifier>/repositories/<repository_id>")
    def repository_page(identifier, repository_id):
        try:
            repo = call("repository_status", project_id=identifier,
                        repository_id=repository_id)
            runtime = call("status")
            return render_template("v2_repository.html", repo=repo,
                                   runtime=runtime, csrf=csrf())
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/projects/<identifier>/repositories/<repository_id>/update")
    def repository_update_form(identifier, repository_id):
        try:
            call("repository_update", project_id=identifier,
                 repository_id=repository_id, expected_head=field("expected_head"))
            flash("Repository fast-forward completed.", "success")
        except (ControllerUnavailable, ControllerRejected) as exc:
            flash(problem(exc)[1], "error")
        return redirect(url_for("repository_page", identifier=identifier,
                                repository_id=repository_id), code=303)

    @app.post("/projects/<identifier>/rename")
    def rename_project(identifier):
        return perform("project_rename", target=identifier,
                       project_id=identifier, name=field("name"))

    @app.post("/projects/<identifier>/compose/config")
    def save_compose_form(identifier):
        content = request.form.get("content", "")
        revision = request.form.get("revision", "")
        if not isinstance(content, str) or len(content) > 48 * 1024:
            abort(413)
        return perform("compose_save", target=identifier,
                       project_id=identifier, content=content, revision=revision)

    @app.post("/projects/<identifier>/settings")
    def save_settings_form(identifier):
        raw = request.form.get("definitions", "")
        if not isinstance(raw, str) or len(raw) > 32 * 1024:
            abort(413)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            flash("Project settings must be valid JSON.", "error")
            return redirect_to(identifier)
        if not isinstance(data, dict) or set(data) != {"applications", "tasks"}:
            abort(400)
        return perform("settings_replace", target=identifier, project_id=identifier,
                       applications=data["applications"], tasks=data["tasks"])

    @app.post("/projects/<identifier>/webhooks")
    def save_webhook_form(identifier):
        try:
            port = int(field("port"))
        except ValueError:
            abort(400)
        return perform("webhook_upsert", target=identifier, project_id=identifier,
                       route=field("route"), service=field("service"),
                       port=port, destination=field("destination"))

    @app.post("/projects/<identifier>/webhooks/<route>/remove")
    def remove_webhook_form(identifier, route):
        return perform("webhook_remove", target=identifier,
                       project_id=identifier, route=route)

    @app.post("/projects/<identifier>/activate")
    def activate(identifier):
        return perform("activate", target=identifier, project_id=identifier)

    @app.post("/projects/<identifier>/switch")
    def switch(identifier):
        return perform("switch", target=identifier, project_id=identifier)

    @app.post("/runtime/deactivate")
    def deactivate():
        return perform("deactivate")

    @app.post("/runtime/reconcile")
    def reconcile():
        return perform("reconcile")

    @app.post("/projects/<identifier>/tasks/<task_name>/run")
    def run_task(identifier, task_name):
        try:
            result = call("task", project_id=identifier, name=task_name)
            return redirect(url_for("job_page", identifier=identifier,
                                    job_id=result["job_id"]), code=303)
        except (ControllerUnavailable, ControllerRejected) as exc:
            flash(problem(exc)[1], "error")
            return redirect_to(identifier)

    @app.post("/projects/<identifier>/compose/<action>")
    def run_compose(identifier, action):
        if action not in {"build", "restart"}:
            abort(404)
        try:
            result = call("compose", project_id=identifier, action=action)
            return redirect(url_for("job_page", identifier=identifier,
                                    job_id=result["job_id"]), code=303)
        except (ControllerUnavailable, ControllerRejected) as exc:
            flash(problem(exc)[1], "error")
            return redirect_to(identifier)

    @app.get("/projects/<identifier>/jobs/<job_id>")
    def job_page(identifier, job_id):
        try:
            job = call("job", project_id=identifier, id=job_id)
            current = call("status")
            return render_template("v2_job.html", job=job, runtime=current,
                                   selected_id=identifier, csrf=csrf())
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects")
    def list_projects():
        try:
            return jsonify(call("projects"))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects")
    def create_project_api():
        payload = json_input({"slug", "name"})
        try:
            result = call("project_create", **payload)
            return jsonify({"project": result}), 201
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/import")
    def import_project_api():
        payload = json_input({"url", "slug", "name"})
        key = request.headers.get("Idempotency-Key", "")
        if not isinstance(key, str) or len(key) != 32:
            abort(400, description="Idempotency-Key header required")
        try:
            result = call("import_submit", **payload, request_key=key)
            response = jsonify({"import": result})
            response.status_code = 202
            response.headers["Location"] = url_for("import_status_api", identifier=result["id"])
            return response
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/imports/<identifier>")
    def import_status_api(identifier):
        try:
            return jsonify(call("import_status", id=identifier))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>")
    def get_project_api(identifier):
        try:
            return jsonify(project_data(identifier))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.patch("/api/v2/projects/<identifier>")
    def rename_project_api(identifier):
        payload = json_input({"name"})
        try:
            return jsonify({"project": call(
                "project_rename", project_id=identifier, name=payload["name"],
            )})
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/compose")
    def compose_config_api(identifier):
        try:
            return jsonify(call("compose_read", project_id=identifier))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.put("/api/v2/projects/<identifier>/compose")
    def save_compose_api(identifier):
        payload = json_input({"content", "revision"})
        try:
            return jsonify(call("compose_save", project_id=identifier, **payload))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/settings")
    def read_settings_api(identifier):
        try:
            return jsonify(call("settings_read", project_id=identifier))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.put("/api/v2/projects/<identifier>/settings")
    def save_settings_api(identifier):
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"applications", "tasks"}:
            abort(400)
        try:
            return jsonify(call("settings_replace", project_id=identifier, **payload))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/webhooks")
    def webhooks_api(identifier):
        try:
            return jsonify(call("webhooks", project_id=identifier))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.put("/api/v2/projects/<identifier>/webhooks/<route>")
    def upsert_webhook_api(identifier, route):
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {
            "service", "port", "destination"
        }:
            abort(400)
        try:
            return jsonify(call("webhook_upsert", project_id=identifier,
                                route=route, **payload))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.delete("/api/v2/projects/<identifier>/webhooks/<route>")
    def delete_webhook_api(identifier, route):
        try:
            return jsonify(call("webhook_remove", project_id=identifier, route=route))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/source-review")
    def project_source_review_api(identifier):
        try:
            return jsonify(call("source_review", project_id=identifier))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/<identifier>/source-review/apply")
    def project_source_apply_api(identifier):
        payload = json_input({"revision", "selected", "overwrites"})
        try:
            return jsonify(call("source_apply", project_id=identifier, **payload))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/repositories/<repository_id>")
    def repository_status_api(identifier, repository_id):
        try:
            return jsonify(call("repository_status", project_id=identifier,
                                repository_id=repository_id))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/<identifier>/repositories/<repository_id>/update")
    def repository_update_api(identifier, repository_id):
        payload = json_input({"expected_head"})
        try:
            return jsonify(call("repository_update", project_id=identifier,
                                repository_id=repository_id, **payload))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/runtime")
    def runtime_api():
        try:
            return jsonify(call("status"))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/<identifier>/activate")
    def activate_api(identifier):
        try:
            return jsonify({"runtime": call("activate", project_id=identifier)})
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/runtime/switch")
    def switch_api():
        payload = json_input({"project_id"})
        try:
            return jsonify({"runtime": call("switch", **payload)})
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/runtime/deactivate")
    def deactivate_api():
        try:
            return jsonify({"runtime": call("deactivate")})
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/runtime/reconcile")
    def reconcile_api():
        try:
            return jsonify({"runtime": call("reconcile")})
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/<identifier>/tasks/<task_name>/run")
    def run_task_api(identifier, task_name):
        try:
            return jsonify(call("task", project_id=identifier, name=task_name)), 202
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/<identifier>/compose/<action>")
    def run_compose_api(identifier, action):
        if action not in {"build", "restart"}:
            abort(404)
        try:
            return jsonify(call("compose", project_id=identifier, action=action)), 202
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/jobs/<job_id>")
    def job_api(identifier, job_id):
        try:
            return jsonify(call("job", project_id=identifier, id=job_id))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    def json_input(keys):
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if (not isinstance(payload, dict) or set(payload) != keys or
                any(not isinstance(value, str) or not value.strip()
                    or len(value) > 2048 for value in payload.values())):
            abort(400)
        return payload

    return app


def main():
    from gunicorn.app.wsgiapp import run
    os.environ.setdefault("GUNICORN_CMD_ARGS", "--bind 127.0.0.1:8765 --workers 2")
    import sys
    sys.argv = [sys.argv[0], "devstack.api.http:create_app()"]
    run()
