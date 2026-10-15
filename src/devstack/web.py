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
from .applications import (
    ApplicationError, add_application, remove_application, update_application,
    load_applications, compose_states, application_status,
    sync_smoke_applications,
)
from .command_runner import (
    CommandError, load_tasks, services as command_services, recent_jobs,
    get_job as command_job_info, job_output as command_job_output,
    start_command, start_task, save_task, delete_task, cancel_command,
)
from .dev_ports import application_urls
from . import runtime
from .webhooks import WebhookError, add_webhook, remove_webhook, load_webhooks
from .project_sync import SyncError, review as sync_review, apply as sync_apply, source_directory
from .template_import import ImportError as TemplateImportError, import_github_project
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


    # Single-project HTTP adapter: project names are not URL parameters.
    # The underlying services are still named internally until PR 02/03.
    project_endpoints = ["webhooks_page","webhooks_add","webhooks_remove","application_create","application_edit","application_remove","commands_page","command_run","command_task_run","command_task_save","command_task_remove","command_details","command_status","command_cancel","docker_project","docker_save","docker_action","docker_status","docker_containers","docker_logs","project_configuration","project_source_update","project_configuration_apply","repo_add","operate","switch","logs"]

    @app.url_value_preprocessor
    def single_project_url(_endpoint, values):
        if _endpoint not in project_endpoints:
            return
        configured = projects()
        if len(configured) != 1:
            abort(404)
        # No route can select a different project's containers.
        values["name"] = next(iter(configured))

    @app.url_defaults
    def single_project_links(endpoint, values):
        if endpoint in project_endpoints:
            values.pop("name", None)

    @app.get("/api/v1/project")
    def project_api():
        configured = projects()
        project = next(iter(configured.values()), None)
        if project is None:
            return {"project": None}
        return {"project": {"name": project.name,
                            "repositories": [repo.name for repo in project.repos]}}

    @app.get("/api/v1/runtime")
    def runtime_api():
        try:
            return runtime.status()
        except runtime.RuntimeControlError as exc:
            return {"phase": "error", "message": str(exc)}, 503

    @app.post("/project/runtime/<action>")
    def runtime_transition(action):
        if action not in ("activate", "deactivate"):
            abort(404)
        if not projects():
            abort(404)
        try:
            runtime.request_transition(action)
            flash(f"Runtime {action} queued. Refresh for current state.", "ok")
        except runtime.RuntimeControlError as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.get("/")
    def index():
        rows = []
        configured = projects()
        for p in configured.values():
            services = [(s.name, service_status(s)) for s in p.services]
            repos = []
            for r in p.repos:
                try:
                    repos.append((r.name, repo_branch(r), repo_dirty(r)))
                except OperationError as exc:
                    repos.append((r.name, f"Unavailable: {exc}", False))
            try:
                applications = load_applications(p.name)
                states = compose_states(p.name) if applications else {}
                apps = [(app, application_status(states, app)) for app in applications]
                app_error = None
            except (ApplicationError, DockerError) as exc:
                apps, app_error = [], str(exc)
            try:
                source_directory(p.name)
                linked_source = True
            except (SyncError, DockerError, OSError):
                linked_source = False
            urls = application_urls(p.name, tuple(a for a, _ in apps))
            rows.append((p, services, repos, apps, app_error, linked_source, urls))
        try:
            lifecycle = runtime.status()
        except runtime.RuntimeControlError as exc:
            lifecycle = {"phase": "error", "message": str(exc)}
        return render_template("dashboard.html", rows=rows, csrf=token(),
                               runtime_state=lifecycle,
                               smoke_installed="smoke" in configured)

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

    @app.get("/project/webhooks")
    def webhooks_page(name):
        project = get_project(name)
        try:
            routes = load_webhooks(name)
            applications = tuple(a for a in load_applications(name) if a.port)
            error = None
        except (WebhookError, ApplicationError, DockerError, OSError) as exc:
            routes, applications, error = (), (), str(exc)
        return render_template("webhooks.html", project=project, routes=routes,
                               applications=applications, error=error, csrf=token())

    @app.post("/project/webhooks/new")
    def webhooks_add(name):
        get_project(name)
        try:
            add_webhook(name, request.form.get("webhook_name", ""),
                        request.form.get("application", ""),
                        request.form.get("path", ""))
            flash("Webhook route added. Public ingress requires separate Funnel opt-in.", "ok")
        except (WebhookError, ApplicationError, DockerError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("webhooks_page", name=name))

    @app.post("/project/webhooks/<route_name>/remove")
    def webhooks_remove(name, route_name):
        get_project(name)
        try:
            remove_webhook(name, route_name)
            flash("Webhook route removed.", "ok")
        except (WebhookError, DockerError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("webhooks_page", name=name))

    @app.post("/project/applications/new")
    def application_create(name):
        get_project(name)
        try:
            add_application(
                name, request.form.get("application_name", ""),
                request.form.get("service", ""),
                request.form.get("port", ""),
            )
            flash("Application added to dashboard. No Docker configuration was changed.", "ok")
        except (ApplicationError, DockerError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index") + "#project-" + name)

    @app.post("/project/applications/<app_name>/edit")
    def application_edit(name, app_name):
        get_project(name)
        try:
            update_application(
                name, app_name, request.form.get("application_name", ""),
                request.form.get("service", ""),
                request.form.get("port", ""),
            )
            flash("Application updated.", "ok")
        except (ApplicationError, DockerError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index") + "#project-" + name)

    @app.post("/project/applications/<app_name>/remove")
    def application_remove(name, app_name):
        get_project(name)
        try:
            remove_application(name, app_name)
            flash("Application removed from the dashboard. Containers were not changed.", "ok")
        except (ApplicationError, DockerError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index") + "#project-" + name)

    @app.get("/project/commands")
    def commands_page(name):
        project = get_project(name)
        try:
            tasks = load_tasks(name)
            history = recent_jobs(name)
            error = None
        except (CommandError, DockerError, OSError, UnicodeError) as exc:
            tasks, history = (), ()
            error = str(exc)
        try:
            choices = command_services(name)
        except (CommandError, DockerError, OSError):
            choices = ()  # Standalone images do not require a Compose file.
        return render_template("commands.html", project=project,
                               choices=choices, tasks=tasks, history=history,
                               error=error, csrf=token())

    @app.post("/project/commands/run")
    def command_run(name):
        get_project(name)
        try:
            identifier = start_command(
                name, request.form.get("service", ""),
                "run" if request.form.get("target") == "image" else request.form.get("mode", "exec"),
                request.form.get("command", ""),
                image=request.form.get("image", "") if request.form.get("target") == "image" else "",
                network=request.form.get("network", "none") if request.form.get("target") == "image" else "none",
            )
            return redirect(url_for("command_details", name=name, identifier=identifier))
        except (CommandError, DockerError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("commands_page", name=name))

    @app.post("/project/commands/tasks/run")
    def command_task_run(name):
        get_project(name)
        try:
            identifier = start_task(name, request.form.get("task", ""))
            return redirect(url_for("command_details", name=name, identifier=identifier))
        except (CommandError, DockerError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("commands_page", name=name))

    @app.post("/project/commands/tasks/save")
    def command_task_save(name):
        get_project(name)
        try:
            save_task(
                name, request.form.get("task", ""),
                request.form.get("service", ""),
                "run" if request.form.get("target") == "image" else request.form.get("mode", "exec"),
                request.form.get("command", ""),
                image=request.form.get("image", "") if request.form.get("target") == "image" else "",
                network=request.form.get("network", "none") if request.form.get("target") == "image" else "none",
            )
            flash("Saved task. Running it requires a separate tap.", "ok")
        except (CommandError, DockerError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("commands_page", name=name))

    @app.post("/project/commands/tasks/remove")
    def command_task_remove(name):
        get_project(name)
        try:
            delete_task(name, request.form.get("task", ""))
            flash("Task removed. Previous command history was preserved.", "ok")
        except (CommandError, DockerError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("commands_page", name=name))

    @app.get("/project/commands/<identifier>")
    def command_details(name, identifier):
        get_project(name)
        try:
            job = command_job_info(name, identifier)
        except CommandError:
            abort(404)
        return render_template("command_output.html", project=name, job=job,
                               csrf=token())

    @app.get("/api/v1/project/commands/<identifier>")
    def command_status(name, identifier):
        get_project(name)
        try:
            return {"job": command_job_info(name, identifier),
                    "output": command_job_output(name, identifier)}
        except CommandError:
            abort(404)

    @app.post("/project/commands/<identifier>/cancel")
    def command_cancel(name, identifier):
        get_project(name)
        try:
            cancel_command(name, identifier)
            flash("Cancellation requested. Existing-container exec may keep running inside Docker.", "ok")
        except CommandError as exc:
            flash(str(exc), "error")
        return redirect(url_for("command_details", name=name, identifier=identifier))

    @app.get("/project/docker")
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

    @app.post("/project/docker/save")
    def docker_save(name):
        get_project(name)
        try:
            save_compose(name, request.form.get("compose", ""))
            flash("Compose definition saved successfully.", "ok")
        except DockerError as exc:
            flash(str(exc), "error")
        return redirect(url_for("docker_project", name=name))

    @app.post("/project/docker/action/<action>")
    def docker_action(name, action):
        get_project(name)
        if action in ("up", "stop", "down"):
            flash("Use project Activate / Deactivate for runtime lifecycle.", "error")
            return redirect(url_for("docker_project", name=name))
        try:
            queue_action(name, action)
            flash(f"Docker {action} queued. Follow progress below.", "ok")
        except DockerError as exc:
            flash(str(exc), "error")
        return redirect(url_for("docker_project", name=name))

    @app.get("/api/v1/project/docker/status")
    def docker_status(name):
        get_project(name)
        job = current_job(name)
        return {"job": job, "log": job_log(name)}

    @app.get("/project/docker/containers")
    def docker_containers(name):
        get_project(name)
        try:
            output = stack_status(name)
        except DockerError as exc:
            output = str(exc)
        return render_template("docker_output.html", project=name, title="Containers",
                               output=output)

    @app.get("/project/docker/logs")
    def docker_logs(name):
        get_project(name)
        try:
            output = stack_logs(name)
        except DockerError as exc:
            output = str(exc)
        return render_template("docker_output.html", project=name, title="Container logs",
                               output=output)

    @app.post("/project/smoke/import")
    def smoke_import():
        if projects():
            abort(409)
        try:
            import_smoke()
            flash("Smoke project imported. Open its Docker Compose page to build and run.", "ok")
        except (DockerError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.post("/project/smoke/sync-applications")
    def smoke_sync_applications():
        if "smoke" not in projects():
            abort(404)
        get_project("smoke")
        try:
            added = sync_smoke_applications()
            flash(
                f"Smoke applications refreshed: {added} default application(s) added. "
                "Existing settings and containers were preserved.",
                "ok",
            )
        except (ApplicationError, DockerError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index") + "#project-smoke")

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

    @app.get("/project/configuration")
    def project_configuration(name):
        project = get_project(name)
        try:
            source = source_directory(name)
            changes, fingerprint = sync_review(name)
            source_label = str(source.relative_to(project.repos[0].path.parent)) if project.repos else str(source)
            error = None
        except (SyncError, DockerError, OSError, ValueError) as exc:
            source_label, changes, fingerprint, error = None, [], None, str(exc)
        return render_template("project_configuration.html",
                               project=project, source_label=source_label,
                               changes=changes, fingerprint=fingerprint,
                               error=error, csrf=token())

    @app.post("/project/configuration/update-source")
    def project_source_update(name):
        project = get_project(name)
        try:
            source = source_directory(name)
            match = next((repo for repo in project.repos
                          if source.is_relative_to(repo.path)), None)
            if match is None:
                raise SyncError("The source repository is not registered")
            branch = repo_update(match)
            flash(f"Source updated on {branch}. Review configuration changes before applying.", "ok")
        except (SyncError, DockerError, OperationError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("project_configuration", name=name))

    @app.post("/project/configuration/apply")
    def project_configuration_apply(name):
        get_project(name)
        try:
            count = sync_apply(name, request.form.getlist("change"),
                               request.form.get("fingerprint", ""))
            flash(f"Applied {count} selected configuration change(s). "
                  "Running containers were not restarted.", "ok")
        except (SyncError, DockerError, OSError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("project_configuration", name=name))

    @app.post("/project/import")
    def project_import():
        if projects():
            abort(409)
        try:
            name, branch = import_github_project(
                request.form.get("url", ""), request.form.get("name", ""),
            )
            flash(f"Added {name} from GitHub" +
                  (f" ({branch})" if branch else "") +
                  ". Review Docker Compose before starting containers.", "ok")
        except (TemplateImportError, ProjectSetupError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.post("/project/new")
    def project_create():
        if projects():
            abort(409)
        try:
            create_project(request.form.get("name", "").strip())
            flash("Project created. Add one or more repositories below.", "ok")
        except (ProjectSetupError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.post("/project/repos/new")
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

    @app.post("/project/action/<action>")
    def operate(name, action):
        if action != "update":
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

    @app.post("/project/repos/<repo_name>/switch")
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

    @app.get("/project/services/<service_name>/logs")
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
