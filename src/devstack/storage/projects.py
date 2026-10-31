"""Transactional registry of multiple independent, inert projects."""

import sqlite3
import json
import time
from uuid import uuid4

from devstack.projects.manifest import application_row
from devstack.domain.models import (
    Project, Repository, project_id, slug, display_name, repo_name, repo_path,
)
from .database import Database


class ProjectNotFound(LookupError):
    pass


class ProjectConflict(RuntimeError):
    pass


def _project(row) -> Project:
    return Project(row["id"], row["slug"], row["name"], row["created_at"])


def _repository(row) -> Repository:
    return Repository(
        row["id"], row["project_id"], row["name"], row["relative_path"],
        row["clone_url"], row["created_at"],
    )


class ProjectRegistry:
    """Metadata only. Importing, browsing and registration never start Docker."""

    def __init__(self, database: Database):
        self.database = database

    def create(self, project_slug: str, name: str) -> Project:
        slug(project_slug)
        display_name(name)
        entry = Project(str(uuid4()), project_slug, name, time.time_ns())
        try:
            with self.database.transaction(write=True) as con:
                con.execute(
                    "INSERT INTO projects(id,slug,name,created_at) VALUES (?,?,?,?)",
                    (entry.id, entry.slug, entry.name, entry.created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise ProjectConflict("Project slug already registered") from exc
        return entry

    def list(self) -> tuple[Project, ...]:
        with self.database.transaction() as con:
            rows = con.execute(
                "SELECT id,slug,name,created_at FROM projects WHERE status='ready' ORDER BY created_at,id"
            ).fetchall()
        return tuple(_project(row) for row in rows)

    def get(self, identifier: str) -> Project:
        project_id(identifier)
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT id,slug,name,created_at FROM projects WHERE id=? AND status='ready'",
                (identifier,),
            ).fetchone()
        if row is None:
            raise ProjectNotFound("Project not registered")
        return _project(row)

    def rename(self, identifier: str, name: str) -> Project:
        project_id(identifier)
        display_name(name)
        with self.database.transaction(write=True) as con:
            updated = con.execute(
                "UPDATE projects SET name=? WHERE id=? AND status='ready'", (name, identifier),
            ).rowcount
            if updated != 1:
                raise ProjectNotFound("Project not registered")
        return self.get(identifier)

    def add_repository(
        self, identifier: str, name: str, relative_path: str, clone_url: str,
    ) -> Repository:
        project_id(identifier)
        repo_name(name)
        repo_path(relative_path)
        item = Repository(
            str(uuid4()), identifier, name, relative_path, clone_url, time.time_ns(),
        )
        try:
            with self.database.transaction(write=True) as con:
                con.execute(
                    "INSERT INTO repositories"
                    "(id,project_id,name,relative_path,clone_url,created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (item.id, item.project_id, item.name, item.relative_path,
                     item.clone_url, item.created_at),
                )
        except sqlite3.IntegrityError as exc:
            try:
                self.get(identifier)
            except ProjectNotFound:
                raise ProjectNotFound("Project not registered") from exc
            raise ProjectConflict("Repository name or path already registered") from exc
        return item

    def repositories(self, identifier: str) -> tuple[Repository, ...]:
        self.get(identifier)
        with self.database.transaction() as con:
            rows = con.execute(
                "SELECT id,project_id,name,relative_path,clone_url,created_at "
                "FROM repositories WHERE project_id=? ORDER BY created_at,id",
                (identifier,),
            ).fetchall()
        return tuple(_repository(row) for row in rows)

    def reserve_import(
        self, identifier: str, project_slug: str, name: str,
        repository_name: str, clone_url: str, branch: str, subdirectory: str,
        applications: tuple, tasks: tuple, *, extra_repositories: tuple = (),
    ) -> Project:
        """Atomically reserve metadata before publishing an imported workspace.

        A reserved project is invisible to ordinary listing and unavailable for
        activation. The provisioning service must publish its filesystem and
        explicitly finalize this reservation.
        """
        project_id(identifier)
        slug(project_slug)
        display_name(name)
        repo_name(repository_name)
        if subdirectory:
            repo_path(subdirectory)
        if not isinstance(branch, str) or len(branch) > 240:
            raise ProjectConflict("Invalid source branch")
        entry = Project(identifier, project_slug, name, time.time_ns())
        repository = Repository(
            str(uuid4()), identifier, repository_name,
            "repos/" + repository_name, clone_url, time.time_ns(),
        )
        try:
            with self.database.transaction(write=True) as con:
                con.execute(
                    "INSERT INTO projects(id,slug,name,created_at,status)"
                    " VALUES(?,?,?,?,'pending')",
                    (entry.id, entry.slug, entry.name, entry.created_at),
                )
                con.execute(
                    "INSERT INTO repositories"
                    "(id,project_id,name,relative_path,clone_url,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (repository.id, repository.project_id, repository.name,
                     repository.relative_path, repository.clone_url,
                     repository.created_at),
                )
                for extra in extra_repositories:
                    repo_name(extra.name)
                    con.execute(
                        "INSERT INTO repositories"
                        "(id,project_id,name,relative_path,clone_url,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (str(uuid4()), identifier, extra.name, "repos/" + extra.name,
                         extra.url, time.time_ns()),
                    )
                con.execute(
                    "INSERT INTO project_sources"
                    "(project_id,repository_id,subdirectory,branch)"
                    " VALUES(?,?,?,?)",
                    (entry.id, repository.id, subdirectory, branch),
                )
                for app in applications:
                    con.execute(
                        "INSERT INTO applications(project_id,name,service,port,url)"
                        " VALUES(?,?,?,?,?)",
                        (entry.id, app.name, app.service, app.port, app.url),
                    )
                for task in tasks:
                    con.execute(
                        "INSERT INTO tasks"
                        "(project_id,name,service,mode,image,network,argv_json)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (entry.id, task.name, task.service, task.mode,
                         task.image, task.network, json.dumps(task.argv)),
                    )
        except sqlite3.IntegrityError as exc:
            raise ProjectConflict("Project or imported metadata already registered") from exc
        return entry

    def pending_imports(self) -> tuple[Project, ...]:
        with self.database.transaction() as con:
            rows = con.execute(
                "SELECT id,slug,name,created_at FROM projects"
                " WHERE status='pending' ORDER BY created_at,id"
            ).fetchall()
        return tuple(_project(row) for row in rows)

    def finalize_import(self, identifier: str) -> Project:
        project_id(identifier)
        with self.database.transaction(write=True) as con:
            updated = con.execute(
                "UPDATE projects SET status='ready'"
                " WHERE id=? AND status='pending'", (identifier,),
            ).rowcount
            if updated != 1:
                raise ProjectNotFound("Pending project not found")
        return self.get(identifier)

    def abort_import(self, identifier: str) -> None:
        """Abort only unpublished metadata; cannot unregister a ready project."""
        project_id(identifier)
        with self.database.transaction(write=True) as con:
            row = con.execute(
                "SELECT status FROM projects WHERE id=?", (identifier,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                raise ProjectConflict("Cannot abort a published or unknown project")
            con.execute("DELETE FROM project_sources WHERE project_id=?", (identifier,))
            con.execute("DELETE FROM applications WHERE project_id=?", (identifier,))
            con.execute("DELETE FROM tasks WHERE project_id=?", (identifier,))
            con.execute("DELETE FROM repositories WHERE project_id=?", (identifier,))
            con.execute("DELETE FROM projects WHERE id=?", (identifier,))

    def import_metadata(self, identifier: str) -> dict:
        """Return linked GitHub source and imported metadata by immutable ID."""
        self.get(identifier)
        with self.database.transaction() as con:
            source = con.execute(
                "SELECT repository_id,subdirectory,branch FROM project_sources"
                " WHERE project_id=?", (identifier,),
            ).fetchone()
            if source is None:
                raise ProjectNotFound("Project has no linked source")
            apps = con.execute(
                "SELECT name,service,port,url FROM applications WHERE project_id=?"
                " ORDER BY name", (identifier,),
            ).fetchall()
            tasks = con.execute(
                "SELECT name,service,mode,image,network,argv_json FROM tasks"
                " WHERE project_id=? ORDER BY name", (identifier,),
            ).fetchall()
        return {
            "repository_id": source["repository_id"],
            "subdirectory": source["subdirectory"],
            "branch": source["branch"],
            "applications": tuple(
                application_row(row["name"], row["service"], row["port"], row["url"])
                for row in apps
            ),
            "tasks": tuple({
                **{key: row[key] for key in ("name", "service", "mode",
                                             "image", "network")},
                "argv": tuple(json.loads(row["argv_json"])),
            } for row in tasks),
        }

    def reserve_empty(self, identifier: str, project_slug: str, name: str) -> Project:
        """Reserve a blank project; its workspace is published before visibility."""
        project_id(identifier)
        slug(project_slug)
        display_name(name)
        entry = Project(identifier, project_slug, name, time.time_ns())
        try:
            with self.database.transaction(write=True) as con:
                con.execute(
                    "INSERT INTO projects(id,slug,name,created_at,status)"
                    " VALUES(?,?,?,?,'pending')",
                    (entry.id, entry.slug, entry.name, entry.created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise ProjectConflict("Project slug already registered") from exc
        return entry
