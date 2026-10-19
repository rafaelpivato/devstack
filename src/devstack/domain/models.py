"""Immutable project identity and validation contracts; no infrastructure imports."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID
import re

_SLUG = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


class DomainError(ValueError):
    """Invalid project or repository metadata."""


def project_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("Invalid project ID") from exc
    if not isinstance(value, str) or parsed.version != 4 or str(parsed) != value:
        raise DomainError("Project ID must be canonical UUIDv4")
    return value


def slug(value: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise DomainError("Invalid project slug")
    return value


def display_name(value: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise DomainError("Invalid display name")
    return value


def repo_name(value: str) -> str:
    if not isinstance(value, str) or not _REPO.fullmatch(value) or ".." in value:
        raise DomainError("Invalid repository name")
    return value


def repo_path(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 512
            or "\" in value or PurePosixPath(value).is_absolute()
            or any(part in ("", ".", "..") for part in value.split("/"))
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise DomainError("Repository path must be relative and traversal-free")
    return value


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    slug: str
    name: str
    created_at: int

    def __post_init__(self) -> None:
        project_id(self.id)
        slug(self.slug)
        display_name(self.name)


@dataclass(frozen=True, slots=True)
class Repository:
    id: str
    project_id: str
    name: str
    relative_path: str
    clone_url: str
    created_at: int

    def __post_init__(self) -> None:
        project_id(self.id)
        project_id(self.project_id)
        repo_name(self.name)
        repo_path(self.relative_path)
        if (not isinstance(self.clone_url, str) or not self.clone_url
                or len(self.clone_url) > 2048 or any(ord(c) < 32 for c in self.clone_url)):
            raise DomainError("Invalid repository clone URL")
