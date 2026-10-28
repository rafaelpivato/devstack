"""DevStack browser entry point and guarded project-scoped HTTP API.

This process owns neither Docker nor the SQLite registry. Every operation
goes over the controller's restricted Unix socket; selecting a project in
one browser tab does not mutate the global active runtime.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import secrets
import re
import sqlite3
from datetime import datetime, timezone

from flask import (
    Flask, abort, jsonify, redirect, render_template, request,
    session, url_for,
)
from werkzeug.exceptions import HTTPException

from devstack.ipc.client import ControllerRejected, ControllerUnavailable, send
from devstack.diagnostics import DiagnosticStore, MAX_EVENTS
from devstack.mcp_diagnostics import snapshot as mcp_diagnostics_snapshot, DEFAULT_SNAPSHOT as MCP_DIAGNOSTICS_PATH
from devstack.mcp_management import MCPManagementError, request_action as request_mcp_action, status as mcp_status
from devstack.oauth import OAuthStore, OAuthError
from devstack.update_control import (
    UpdateControlError, installed_version, latest_version, update_state,
    request_update, updater_service_events,
)

SAFE_READ = {"GET", "HEAD", "OPTIONS"}
NOT_FOUND = {"TransitionNotFound", "WebhookNotFound", "ProjectNotFound", "ImportNotFound", "RepositoryNotFound", "RepositoryAddNotFound", "BulkRepositoryNotFound"}
INVALID = {"WebhookInvalid", "ConfigurationInvalid", "DomainError", "ProtocolError", "GitHubSourceError",
           "ManifestError", "ProjectProvisionError", "WorkspaceError", "RepositoryGitInvalid", "SourceReviewInvalid", "GitHubSSHError", "JobInvalid", "RepositorySyncInvalid", "ReactionInvalid"}
CONFLICT = {"TransitionConflict", "WebhookBusy", "ConfigurationConflict", "ControlError", "ProjectConflict", "JobError", "DockerControlError", "ImportConflict", "RepositoryGitError", "SourceSyncConflict", "RepositoryAddConflict", "BulkRepositoryConflict"}


def create_app(config: dict | None = None, *, transport=None) -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(
        SECRET_KEY=os.environ.get("DEVSTACK_SECRET_KEY") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.environ.get("DEVSTACK_SECURE_COOKIES", "1") == "1",
        MAX_CONTENT_LENGTH=64 * 1024,
        WEB_DIAGNOSTICS_PATH=os.environ.get(
            "DEVSTACK_WEB_DIAGNOSTICS_PATH", "/var/lib/devstack-web/diagnostics.sqlite3"),
        WEB_DIAGNOSTICS_FALLBACK_PATH=os.environ.get(
            "DEVSTACK_WEB_DIAGNOSTICS_FALLBACK_PATH",
            "/run/devstack-update-request/web-diagnostics.sqlite3"),
        MCP_DIAGNOSTICS_PATH=os.environ.get("DEVSTACK_MCP_DIAGNOSTICS_PATH", str(MCP_DIAGNOSTICS_PATH)),
        OAUTH_DB_PATH=os.environ.get("DEVSTACK_OAUTH_DB_PATH", "/var/lib/devstack-oauth/oauth.sqlite3"),
        CONTROLLER_SOCKET=os.environ.get(
            "DEVSTACK_CONTROL_SOCKET", "/run/devstack-control/control.sock"),
    )
    if config:
        app.config.update(config)

    def call(op: str, **fields):
        if transport is not None:
            return transport(op, **fields)
        timeout = (240 if op in ("branch_preview", "branch_apply")
                   else 210 if op == "job_cancel"
                   else 60 if op == "github_ssh_test" else 30)
        return send(Path(app.config["CONTROLLER_SOCKET"]), op,
                    timeout=timeout, **fields)

    def web_stores():
        # RuntimeDirectory was previously exercised with the real web UID in
        # Ubuntu CI. Use it if the persistent StateDirectory is unavailable.
        for name in ("WEB_DIAGNOSTICS_PATH", "WEB_DIAGNOSTICS_FALLBACK_PATH"):
            path = Path(app.config[name])
            if name == "WEB_DIAGNOSTICS_FALLBACK_PATH" and path == Path(app.config["WEB_DIAGNOSTICS_PATH"]):
                continue
            yield name, DiagnosticStore(path)

    def record_web(code: str, *, source: str = "web", level: str = "info"):
        for _, store in web_stores():
            try:
                store.record(source, code, level)
                return
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                continue  # Never break a request due to diagnostic storage.


    record_web("web-started")

    @app.after_request
    def record_http_failure(response):
        if response.status_code >= 400 and request.path.startswith("/api/v2/"):
            record_web("web-error", level="error" if response.status_code >= 500 else "warning")
        return response

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

    def release_status(check_latest=False):
        """Unprivileged release metadata only; no controller shell capability."""
        installed = installed_version()
        update = update_state()
        latest = None
        error = None
        if check_latest:
            record_web("web-update-check", source="updater")
            try:
                latest = latest_version()
            except UpdateControlError:
                record_web("web-update-check-failed", source="updater", level="warning")
                error = "Unable to check published GitHub releases."
        newer = (
            latest is not None and
            tuple(map(int, latest[1:].split("."))) >
            tuple(map(int, installed[1:].split(".")))
        )
        return {"installed": installed, "latest": latest,
                "update_available": newer, "update": update,
                "check_error": error}

    def queue_release_update():
        # Root updater performs an independent inactive/healthy preflight.
        state = call("status")
        if state["phase"] != "inactive" or state["project_id"] is not None:
            raise UpdateControlError("Deactivate the active project before updating DevStack.")
        latest = latest_version()
        installed = installed_version()
        if tuple(map(int, latest[1:].split("."))) <= tuple(
                map(int, installed[1:].split("."))):
            raise UpdateControlError("Already on the latest published release.")
        result = request_update()
        record_web("web-update-request", source="updater")
        return result

    @app.get("/api/v2/devstack/update-status")
    def devstack_update_status_api():
        return jsonify(release_status(request.args.get("check") == "1"))

    @app.post("/api/v2/devstack/update-now")
    def devstack_update_api():
        if request.content_length and not request.is_json:
            abort(415)
        if request.is_json and request.get_json(silent=True) != {}:
            abort(400)
        try:
            return jsonify({"message": queue_release_update()}), 202
        except UpdateControlError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 409
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/devstack/diagnostics")
    def devstack_diagnostics_api():
        """Private read-only aggregate: selected metadata, not host journald text."""
        raw = request.args.get("minutes", "5")
        if raw not in {"1", "5", "30"}:
            return jsonify({"ok": False, "message": "Window must be 1, 5 or 30 minutes"}), 400
        minutes = int(raw)
        sources = {"controller": "unavailable", "runtime": "unavailable",
                   "web": "unavailable", "updater": "unavailable", "mcp": "unavailable"}
        events = []
        truncated = False
        try:
            result = call("diagnostics_snapshot", minutes=minutes)
            events.extend(result["events"])
            sources.update({k: result["sources"][k] for k in ("controller", "runtime")})
            truncated = bool(result["truncated"])
        except (ControllerUnavailable, ControllerRejected, KeyError, ValueError, TypeError):
            pass
        # Read both stores when available: brief permission failures can have
        # put recent events into the volatile fallback.
        web_primary = False
        web_fallback = False
        for name, store in web_stores():
            try:
                data = store.snapshot(minutes)
                events.extend(data["events"])
                truncated = truncated or data["truncated"]
                if name == "WEB_DIAGNOSTICS_PATH":
                    web_primary = True
                else:
                    web_fallback = True
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                pass
        if web_primary:
            sources["web"] = "available"
        elif web_fallback:
            sources["web"] = "partial (volatile fallback)"
        safe_update = None
        try:
            data = update_state()
            valid_phases = {"idle", "queued", "updating", "failed"}
            valid_units = {"inactive", "active", "activating", "deactivating", "failed", "unknown"}
            phase = data.get("phase", "unknown")
            unit = data.get("unit_state", "unknown")
            safe_update = {
                "phase": phase if phase in valid_phases else "unknown",
                "unit_state": unit if unit in valid_units else "unknown",
                "last_result": re.sub(r"[^a-zA-Z0-9_-]", "", data.get("last_result", ""))[:40],
                "last_exit_code": re.sub(r"[^0-9-]", "", data.get("last_exit_code", ""))[:8],
            }
            history = updater_service_events(minutes)
            events.extend(history["events"])
            sources["updater"] = "available" if history["available"] else "partial (status only)"
        except (OSError, ValueError, TypeError):
            sources["updater"] = "unavailable"
        mcp = mcp_diagnostics_snapshot(
            minutes, Path(app.config["MCP_DIAGNOSTICS_PATH"]))
        events.extend(mcp["events"])
        truncated = truncated or mcp["truncated"]
        sources["mcp"] = "available" if mcp["available"] else "unavailable"
        # Always sort chronologically and clearly report clipping/partial sources.
        events = [event for event in events if isinstance(event, dict) and
                  isinstance(event.get("at"), int)]
        events.sort(key=lambda row: (row["at"], row.get("source", ""), row.get("code", "")))
        if len(events) > MAX_EVENTS:
            events = events[-MAX_EVENTS:]
            truncated = True
        return jsonify({
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "version": installed_version(), "minutes": minutes,
            "events": events, "truncated": truncated,
            "sources": sources, "updater_status": safe_update,
            "partial": any(value != "available" for value in sources.values()),
        })

    @app.get("/api/v2/devstack/mcp")
    def mcp_status_api():
        return jsonify(mcp_status())

    @app.post("/api/v2/devstack/mcp/<action>")
    def mcp_action_api(action):
        if action not in {"enable", "disable", "disable_webhooks", "enable_webhooks"}:
            abort(404)
        if request.is_json and request.get_json(silent=True) != {}:
            abort(400)
        if request.content_length and not request.is_json:
            abort(415)
        try:
            request_mcp_action(action)
            record_web("web-mcp-" + action + "-requested")
        except MCPManagementError as exc:
            record_web("web-mcp-request-failed", level="warning")
            return jsonify({"ok": False, "message": "MCP request not queued: " + exc.code}), 409
        return jsonify({"message": "MCP " + action + " queued", "request_pending": True}), 202

    # Only the Tailscale-private Web process serves approval and revocation.
    # Public OAuth endpoints cannot approve their own authorization requests.
    def oauth_store():
        return OAuthStore(Path(app.config["OAUTH_DB_PATH"]))

    def oauth_failure(exc):
        code = exc.code if isinstance(exc, OAuthError) else "store_unavailable"
        status = 404 if code in ("request_expired", "not_found") else \
                 503 if code == "store_unavailable" else 409
        return jsonify({"ok": False, "message": code}), status

    @app.get("/api/v2/devstack/mcp/oauth/requests/<identifier>")
    def oauth_request_detail(identifier):
        try:
            return jsonify(oauth_store().pending(identifier))
        except (OAuthError, OSError, sqlite3.Error) as exc:
            return oauth_failure(exc)

    @app.post("/api/v2/devstack/mcp/oauth/requests/<identifier>/<decision>")
    def oauth_decide(identifier, decision):
        if decision not in {"approve", "deny"}:
            abort(404)
        if not request.is_json or request.get_json(silent=True) != {}:
            abort(400)
        try:
            # The external issuer is the actual pinned HTTPS endpoint, not a
            # public request's Host/X-Forwarded headers.
            info = mcp_status()
            url = info.get("url")
            if not isinstance(url, str) or not url.startswith("https://") or \
                    not url.endswith("/mcp"):
                raise OAuthError("store_unavailable")
            redirect_url = oauth_store().decide(identifier, decision == "approve",
                                                url[:-4])
            record_web("web-mcp-oauth-approved" if decision == "approve"
                       else "web-mcp-oauth-denied")
            return jsonify({"redirect_url": redirect_url})
        except (OAuthError, OSError, sqlite3.Error) as exc:
            return oauth_failure(exc)

    @app.get("/api/v2/devstack/mcp/oauth/connections")
    def oauth_connections_api():
        try:
            return jsonify({"connections": oauth_store().connections()})
        except (OAuthError, OSError, sqlite3.Error) as exc:
            return oauth_failure(exc)

    @app.post("/api/v2/devstack/mcp/oauth/connections/<identifier>/revoke")
    def oauth_revoke_api(identifier):
        if not request.is_json or request.get_json(silent=True) != {}:
            abort(400)
        try:
            oauth_store().revoke(identifier)
            record_web("web-mcp-oauth-revoked")
            return jsonify({"revoked": True})
        except (OAuthError, OSError, sqlite3.Error) as exc:
            return oauth_failure(exc)

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
        """Only dashboard: loading this GET does not contact or mutate the controller."""
        bundle = Path(app.static_folder) / "v06"
        try:
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            entry = manifest["src/main.tsx"]
            js_asset = entry["file"]
            css_assets = entry.get("css") or [
                item["file"] for item in manifest.values()
                if isinstance(item, dict) and
                isinstance(item.get("file"), str) and
                item["file"].endswith(".css")
            ]
            # Only versioned local, packaged asset paths may be inserted.
            paths = [js_asset, *css_assets]
            if (not isinstance(css_assets, list) or
                    not all(isinstance(path, str) and path.startswith("assets/")
                            and ".." not in Path(path).parts
                            and (bundle / path).is_file() for path in paths)):
                raise ValueError("Invalid packaged bundle")
        except (OSError, ValueError, TypeError, KeyError):
            return jsonify({"ok": False, "message": "Frontend bundle unavailable."}), 503
        return render_template("app.html", csrf=csrf(),
                               js_asset=js_asset, css_assets=css_assets)

    def valid_frontend_path(frontend_path):
        """Only known project-scoped SPA routes; no generic catch-all."""
        import re
        pattern = (r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                   r"[89ab][0-9a-f]{3}-[0-9a-f]{12}")
        segments = frontend_path.split("/") if frontend_path else []
        valid = (
            not segments or
            segments == ["projects"] or
            segments in (["projects","create"],["projects","import"]) or
            (len(segments)==2 and segments[0]=="imports" and
             re.fullmatch(pattern,segments[1],re.IGNORECASE) is not None) or
            segments == ["administration"] or
            segments in (["administration", "ssh"], ["administration", "updates"],
                         ["administration", "diagnostics"], ["administration", "mcp"],
                          ["administration", "mcp", "authorize"]) or
            segments in (["runtime", "deactivate"], ["runtime", "reconcile"],
                         ["runtime", "operations"]) or
            (len(segments) == 3 and segments[:2] == ["runtime", "operations"]
             and re.fullmatch(pattern, segments[2], re.IGNORECASE) is not None) or
            (len(segments) >= 2 and segments[0] == "projects" and
             re.fullmatch(pattern, segments[1], re.IGNORECASE) is not None and
             (len(segments) == 2 or
              (len(segments) == 3 and segments[2] in {
                  "repositories", "services", "jobs", "applications", "configuration",
              }) or
              (len(segments) == 4 and segments[2] == "runtime" and
               segments[3] in {"activate", "switch"}) or
              (len(segments) == 4 and segments[2] == "configuration" and
               segments[3] in {"compose", "definitions", "webhooks", "name"}) or
              (len(segments) == 4 and segments[2] == "services" and
               re.fullmatch(r"[a-f0-9]{64}", segments[3]) is not None) or
              (len(segments) == 4 and segments[2] == "jobs" and
               re.fullmatch(pattern, segments[3], re.IGNORECASE) is not None) or
              (len(segments) == 5 and segments[2] == "jobs" and
               ((segments[3] == "run" and segments[4] in {
                   "task", "compose", "adhoc",
               }) or
                (re.fullmatch(pattern, segments[3], re.IGNORECASE) is not None
                 and segments[4] == "logs"))) or
              (len(segments) == 4 and segments[2] == "repositories" and
               segments[3] in {"add", "bulk", "branch", "source"}) or
              (len(segments) == 5 and segments[2] == "repositories" and
               segments[3] in {"adds", "updates"} and
               re.fullmatch(pattern, segments[4], re.IGNORECASE) is not None) or
              (len(segments) == 4 and segments[2] == "repositories" and
               re.fullmatch(pattern, segments[3], re.IGNORECASE) is not None)))
        )
        return valid

    @app.get("/<path:frontend_path>")
    def react_deep_link(frontend_path=""):
        if not valid_frontend_path(frontend_path):
            abort(404)
        return index()

    # Migrate saved preview bookmarks without retaining the old HTML UI.
    @app.get("/__v06")
    @app.get("/__v06/")
    def old_preview_root():
        return redirect("/", code=308)

    @app.get("/__v06/<path:frontend_path>")
    def old_preview_deep_link(frontend_path):
        # Invalid/unrecognized paths must never become an unrestricted redirect.
        # Check the same allowlist against the new canonical URL.
        if not valid_frontend_path(frontend_path):
            abort(404)
        return redirect("/" + frontend_path, code=308)

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
            return jsonify(call("project_detail", project_id=identifier))
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

    @app.get("/api/v2/projects/<identifier>/settings-review")
    def read_settings_review_api(identifier):
        try:
            return jsonify(call("settings_review", project_id=identifier))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.put("/api/v2/projects/<identifier>/settings-review")
    def save_settings_review_api(identifier):
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {
            "applications", "tasks", "revision"
        } or not isinstance(payload["applications"], list) or not isinstance(payload["tasks"], list):
            abort(400)
        if not isinstance(payload["revision"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", payload["revision"]
        ):
            abort(400)
        try:
            result = call("settings_replace_checked", project_id=identifier, **payload)
            return jsonify(result)
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

    @app.get("/api/v2/github/ssh")
    def github_ssh_status_api():
        try:
            return jsonify(call("github_ssh_status"))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/github/ssh/generate")
    def github_ssh_generate_api():
        try:
            return jsonify(call("github_ssh_generate"))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/github/ssh/test")
    def github_ssh_test_api():
        try:
            return jsonify(call("github_ssh_test"))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/<identifier>/repositories")
    def add_repository_api(identifier):
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if (not isinstance(payload, dict)
                or set(payload) - {"url", "name", "branch"}
                or not {"url", "name"}.issubset(payload)
                or any(not isinstance(value, str) or len(value) > 2048
                       for value in payload.values())
                or not payload["url"].strip() or not payload["name"].strip()):
            abort(400)
        payload.setdefault("branch", "")
        key = request.headers.get("Idempotency-Key", "")
        if not isinstance(key, str) or len(key) != 32:
            abort(400, description="Idempotency-Key header required")
        try:
            result = call("repository_add_submit", project_id=identifier,
                          **payload, request_key=key)
            response = jsonify({"import": result})
            response.status_code = 202
            response.headers["Location"] = url_for(
                "repository_add_status_api", identifier=identifier,
                operation_id=result["id"])
            return response
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/repository-adds/<operation_id>")
    def repository_add_status_api(identifier, operation_id):
        try:
            return jsonify(call("repository_add_status", project_id=identifier,
                                id=operation_id))
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
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {
            "revision", "selected", "overwrites"
        }:
            abort(400)
        try:
            return jsonify(call("source_apply", project_id=identifier, **payload))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/git/branch-preview")
    def branch_preview_api(identifier):
        branch = request.args.get("branch", "")
        if len(branch) > 200:
            abort(400)
        try:
            return jsonify(call("branch_preview", project_id=identifier,
                                branch=branch,
                                repository_id=request.args.get("repository_id") or None))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/<identifier>/git/branch-apply")
    def branch_apply_api(identifier):
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if (not isinstance(payload, dict) or
                set(payload) != {"branch", "repository_id", "revision"}):
            abort(400)
        try:
            return jsonify(call("branch_apply", project_id=identifier, **payload))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/<identifier>/repositories/update-all")
    def repository_bulk_api(identifier):
        key = request.headers.get("Idempotency-Key", "")
        if not isinstance(key, str) or len(key) != 32:
            abort(400)
        if request.is_json:
            payload = request.get_json(silent=True)
            if payload != {}:
                abort(400)
        elif request.content_length:
            abort(415)
        try:
            result = call("repository_bulk_submit", project_id=identifier,
                          request_key=key)
            response = jsonify(result)
            response.status_code = 202
            response.headers["Location"] = url_for(
                "repository_bulk_status_api", identifier=identifier,
                operation_id=result["id"],
            )
            return response
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/repository-updates/<operation_id>")
    def repository_bulk_status_api(identifier, operation_id):
        try:
            return jsonify(call("repository_bulk_status", project_id=identifier,
                                id=operation_id))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/repositories/<repository_id>")
    def repository_status_api(identifier, repository_id):
        try:
            return jsonify(call("repository_status", project_id=identifier,
                                repository_id=repository_id))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/repositories/<repository_id>/sync")
    def repository_sync_state_api(identifier, repository_id):
        try:
            return jsonify(call("repository_sync_state", project_id=identifier,
                                repository_id=repository_id))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.put("/api/v2/projects/<identifier>/repositories/<repository_id>/sync")
    def repository_sync_policy_api(identifier, repository_id):
        payload = json_input({"policy"})
        try:
            return jsonify(call("repository_sync_policy", project_id=identifier,
                                repository_id=repository_id, **payload))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/repositories/<repository_id>/reactions")
    def repository_reactions_api(identifier, repository_id):
        try:
            return jsonify(call("repository_reactions", project_id=identifier,
                                repository_id=repository_id))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.put("/api/v2/projects/<identifier>/repositories/<repository_id>/reactions")
    def repository_reaction_policy_api(identifier, repository_id):
        payload = json_input({"service", "action"})
        try:
            return jsonify(call("repository_reaction_policy", project_id=identifier,
                                repository_id=repository_id, **payload))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/<identifier>/repositories/<repository_id>/check")
    def repository_check_api(identifier, repository_id):
        if not request.is_json or request.get_json(silent=True) != {}:
            abort(400)
        try:
            return jsonify(call("repository_check", project_id=identifier,
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

    @app.post("/api/v2/runtime/transitions")
    def runtime_transition_submit_api():
        """Submit only the existing controller-owned durable transition contract."""
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"action", "project_id"}:
            abort(400)
        action = payload["action"]
        target = payload["project_id"]
        if not isinstance(action, str) or action not in {
            "activate", "switch", "deactivate", "reconcile",
        }:
            abort(400)
        from uuid import UUID
        if action in {"activate", "switch"}:
            try:
                if (not isinstance(target, str) or
                        str(UUID(target, version=4)) != target):
                    abort(400)
            except (TypeError, ValueError, AttributeError):
                abort(400)
        elif target is not None:
            abort(400)
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[0-9a-f]{32}", key):
            abort(400)
        try:
            receipt = call("transition_submit", action=action, project_id=target,
                           request_key=key)
            response = jsonify({"operation": receipt})
            response.status_code = 202
            response.headers["Location"] = url_for(
                "runtime_transition_status_api", operation_id=receipt["id"])
            return response
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/runtime/transitions")
    def runtime_transition_list_api():
        try:
            return jsonify(call("transition_list"))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/runtime/transitions/<operation_id>")
    def runtime_transition_status_api(operation_id):
        try:
            from uuid import UUID
            if str(UUID(operation_id, version=4)) != operation_id:
                abort(404)
        except (ValueError, TypeError):
            abort(404)
        try:
            return jsonify(call("transition_status", id=operation_id))
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

    @app.post("/api/v2/projects/<identifier>/services/<service>/actions/<action>")
    def targeted_compose_api(identifier, service, action):
        if action not in {"restart", "redeploy"}:
            abort(404)
        try:
            return jsonify(call("compose_service", project_id=identifier,
                                service=service, action=action)), 202
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/<identifier>/adhoc")
    def submit_adhoc_api(identifier):
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {
            "kind", "service", "image", "network", "command"
        }:
            abort(400)
        try:
            return jsonify(call("adhoc", project_id=identifier, **payload)), 202
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/private-ports")
    def private_ports_api(identifier):
        try:
            return jsonify(call("private_ports", project_id=identifier))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/services")
    def services_api(identifier):
        try:
            return jsonify(call("services", project_id=identifier))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/services/<container_id>/logs")
    def service_logs_api(identifier, container_id):
        try:
            tail = int(request.args.get("tail", "150"))
        except (ValueError, TypeError):
            abort(400)
        if not 1 <= tail <= 200:
            abort(400)
        try:
            return jsonify(call("service_logs", project_id=identifier,
                                container_id=container_id, tail=tail))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/jobs")
    def jobs_history_api(identifier):
        try:
            return jsonify(call("jobs", project_id=identifier))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.get("/api/v2/projects/<identifier>/jobs/<job_id>/logs")
    def job_logs_api(identifier, job_id):
        try:
            offset = int(request.args.get("offset", "0"))
            if offset < 0 or offset > 2 * 1024 * 1024:
                abort(400)
        except ValueError:
            abort(400)
        try:
            return jsonify(call("job_logs", project_id=identifier, id=job_id,
                                offset=offset))
        except (ControllerUnavailable, ControllerRejected) as exc:
            return json_error(exc)

    @app.post("/api/v2/projects/<identifier>/jobs/<job_id>/cancel")
    def cancel_job_api(identifier, job_id):
        try:
            return jsonify(call("job_cancel", project_id=identifier, id=job_id))
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
