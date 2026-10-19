"""Transactional registry of multiple independent, inert projects."""

import sqlite3
import time
from uuid import uuid4

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
                "SELECT id,slug,name,created_at FROM projects ORDER BY created_at,id"
            ).fetchall()
        return tuple(_project(row) for row in rows)

    def get(self, identifier: str) -> Project:
        project_id(identifier)
        with self.database.transaction() as con:
            row = con.execute(
                "SELECT id,slug,name,created_at FROM projects WHERE id=?",
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
                "UPDATE projects SET name=? WHERE id=?", (name, identifier),
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
