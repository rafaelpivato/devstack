"""Browser UI. Bind to loopback; publish only through an authenticated private proxy."""

from __future__ import annotations

import os
import secrets

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

from .update_control import (
    UpdateControlError, installed_version, latest_version, update_state,
    check_request_directory, request_update,
)
from .docker_stacks import (
    DockerError, docker_available, read_compose, save_compose, stack_status,
    stack_logs, current_job, job_log, queue_action, import_smoke,
)
from .github_ssh import GitHubSSHError, generate_key, public_key, test_connection
from .project_setup import ProjectSetupError, create_project, add_github_repository

from .core import (
    OperationError, load_projects, project_action, repo_branch, repo_dirty,
    repo_switch, repo_update, service_logs, service_status,
)


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("DEVSTACK_SECRET_KEY") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.environ.get("DEVSTACK_SECURE_COOKIES", "0") == "1",
        SESSION_COOKIE_SAMESITE="Strict",
    )
    if config:
        app.config.update(config)

    def projects():
        return load_projects()

    def get_project(name):
        project = projects().get(name)
        if project is None:
            abort(404)
        return project

    def token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(32)
        return session["_csrf"]

    @app.before_request
    def protect_writes():
        if request.method == "POST" and (
            not session.get("_csrf")
            or not request.form.get("_csrf")
            or not secrets.compare_digest(session["_csrf"], request.form["_csrf"])
        ):
            abort(403)

    @app.get("/")
    def index():
        rows = []
        for p in projects().values():
            services = [(s.name, service_status(s)) for s in p.services]
            repos = []
            for r in p.repos:
                try:
                    repos.append((r.name, repo_branch(r), repo_dirty(r)))
                except OperationError as exc:
                    repos.append((r.name, f"Unavailable: {exc}", False))
            rows.append((p, services, repos))
        return render_template("dashboard.html", rows=rows, csrf=token())

    @app.get("/api/devstack/update-status")
    def devstack_update_status():
        """Polling endpoint; GitHub outages never prevent showing local state."""
        status = update_state()
        status["installed"] = installed_version()
        try:
            status["latest"] = latest_version()
        except UpdateControlError as exc:
            status["latest"] = None
            status["release_error"] = str(exc)
        try:
            check_request_directory()
            status["manual_available"] = True
        except UpdateControlError as exc:
            status["manual_available"] = False
            status["setup_error"] = str(exc)
        return status

    @app.post("/api/devstack/update-now")
    def devstack_update_now():
        try:
            message = request_update()
        except UpdateControlError as exc:
            return {"ok": False, "message": str(exc)}, 503
        return {"ok": True, "message": message}, 202

    @app.get("/projects/<name>/docker")
    def docker_project(name):
        project = get_project(name)
        try:
            content = read_compose(name)
            job = current_job(name)
        except DockerError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        return render_template(
            "docker_project.html", project=project, compose=content,
            docker_ready=docker_available(), job=job, csrf=token(),
        )

    @app.post("/projects/<name>/docker/save")
    def docker_save(name):
        get_project(name)
        try:
            save_compose(name, request.form.get("compose", ""))
            flash("Compose definition saved successfully.", "ok")
        except DockerError as exc:
            flash(str(exc), "error")
        return redirect(url_for("docker_project", name=name))

    @app.post("/projects/<name>/docker/action/<action>")
    def docker_action(name, action):
        get_project(name)
        try:
            queue_action(name, action)
            flash(f"Docker {action} queued. Follow progress below.", "ok")
        except DockerError as exc:
            flash(str(exc), "error")
        return redirect(url_for("docker_project", name=name))

    @app.get("/api/projects/<name>/docker/status")
    def docker_status(name):
        get_project(name)
        job = current_job(name)
        return {"job": job, "log": job_log(name)}

    @app.get("/projects/<name>/docker/containers")
    def docker_containers(name):
        get_project(name)
        try:
            output = stack_status(name)
        except DockerError as exc:
            output = str(exc)
        return render_template("docker_output.html", project=name, title="Containers",
                               output=output)

    @app.get("/projects/<name>/docker/logs")
    def docker_logs(name):
        get_project(name)
        try:
            output = stack_logs(name)
        except DockerError as exc:
            output = str(exc)
        return render_template("docker_output.html", project=name, title="Container logs",
                               output=output)

    @app.post("/projects/smoke/import")
    def smoke_import():
        try:
            import_smoke()
            flash("Smoke project imported. Open its Docker Compose page to build and run.", "ok")
        except (DockerError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.get("/settings/github")
    def github_settings():
        try:
            key = public_key()
        except GitHubSSHError as exc:
            flash(str(exc), "error")
            key = None
        return render_template("github_settings.html", public_key=key, csrf=token())

    @app.post("/settings/github/generate")
    def github_generate():
        try:
            generate_key()
            flash("GitHub SSH key generated. Add the public key to your GitHub account.", "ok")
        except GitHubSSHError as exc:
            flash(str(exc), "error")
        return redirect(url_for("github_settings"))

    @app.post("/settings/github/test")
    def github_test():
        try:
            account = test_connection()
            flash(f"GitHub SSH authenticated as {account}.", "ok")
        except GitHubSSHError as exc:
            flash(str(exc), "error")
        return redirect(url_for("github_settings"))

    @app.post("/projects/new")
    def project_create():
        try:
            create_project(request.form.get("name", "").strip())
            flash("Project created. Add one or more repositories below.", "ok")
        except (ProjectSetupError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.post("/projects/<name>/repos/new")
    def repo_add(name):
        try:
            result = add_github_repository(
                name, request.form.get("url", ""),
                repo_name=request.form.get("name", ""),
                branch=request.form.get("branch", ""),
            )
            flash(f"Cloned repository {result} successfully.", "ok")
        except (ProjectSetupError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.post("/projects/<name>/<action>")
    def operate(name, action):
        if action not in ("start", "stop", "update"):
            abort(404)
        project = get_project(name)
        try:
            if action == "update":
                for repo in project.repos:
                    repo_update(repo)
            else:
                project_action(project, action)
            flash(f"{project.name}: {action} completed", "ok")
        except OperationError as exc:
            flash(f"{project.name}: {exc}", "error")
        return redirect(url_for("index"))

    @app.post("/projects/<name>/repos/<repo_name>/switch")
    def switch(name, repo_name):
        project = get_project(name)
        repo = next((r for r in project.repos if r.name == repo_name), None)
        if repo is None:
            abort(404)
        try:
            repo_switch(repo, request.form.get("branch", ""))
            flash(f"{repo_name}: switched branch", "ok")
        except OperationError as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.get("/projects/<name>/services/<service_name>/logs")
    def logs(name, service_name):
        project = get_project(name)
        service = next((s for s in project.services if s.name == service_name), None)
        if service is None:
            abort(404)
        try:
            output = service_logs(service)
        except OperationError as exc:
            output = f"Error fetching logs: {exc}"
        return render_template("logs.html", project=project, service=service,
                               output=output)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    return app


def main():
    app = create_app()
    # Never expose the administrator UI to the public Internet directly.
    app.run(host=os.environ.get("DEVSTACK_HOST", "127.0.0.1"),
            port=int(os.environ.get("DEVSTACK_PORT", "8765")), debug=False)


if __name__ == "__main__":
    main()
