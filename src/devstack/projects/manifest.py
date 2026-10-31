"""Parse declarative GitHub template metadata without importing runtime code."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import re
import tomllib

from devstack.adapters.github import GitHubSourceError, parse_github_url

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_MAX_FILE = 128 * 1024


class ManifestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ApplicationSpec:
    name: str
    service: str = ""
    port: int | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    service: str
    mode: str
    image: str
    network: str
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepositorySpec:
    name: str
    url: str
    branch: str


@dataclass(frozen=True, slots=True)
class ProjectTemplate:
    applications: tuple[ApplicationSpec, ...]
    tasks: tuple[TaskSpec, ...]
    managed_files: dict[str, bytes]
    repositories: tuple[RepositorySpec, ...] = ()


def _name(value: object, label: str, *, max_length: int = 80) -> str:
    if (not isinstance(value, str) or not value or len(value) > max_length
            or not _NAME.fullmatch(value) or ".." in value):
        raise ManifestError(f"Invalid {label}")
    return value


def _read(directory: Path, filename: str, *, required: bool = False) -> bytes | None:
    path = directory / filename
    if path.is_symlink():
        raise ManifestError("Symlinked template file is forbidden")
    if not path.exists():
        if required:
            raise ManifestError(f"Missing {filename}")
        return None
    if not path.is_file() or path.stat().st_size > _MAX_FILE:
        raise ManifestError(f"Invalid or oversized {filename}")
    return path.read_bytes()


def _toml(raw: bytes) -> dict:
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError("Invalid TOML in GitHub project manifest") from exc
    if not isinstance(parsed, dict):
        raise ManifestError("Expected a TOML table")
    return parsed


def validate_application_url(value: object) -> str:
    """Application bookmarks are explicit URLs, never Docker-discovered.

    {host} may replace the entire hostname; the web client substitutes the
    hostname by which the owner opened their DevStack workstation.
    """
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or value != value.strip()
            or any(ord(ch) < 33 or ord(ch) == 127 for ch in value)
            or "\\" in value):
        raise ManifestError("Invalid application URL")
    if "{host}" in value:
        if value.count("{host}") != 1:
            raise ManifestError("Invalid application host placeholder")
        candidate = value.replace("{host}", "devstack.example")
    else:
        candidate = value
    if "{" in candidate or "}" in candidate:
        raise ManifestError("Unknown application URL placeholder")
    try:
        parsed = urlsplit(candidate)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or not parsed.netloc or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.query):
            raise ValueError("Unsafe application URL")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid application port")
        if value.count("{host}") and parsed.hostname != "devstack.example":
            raise ValueError("Placeholder must replace the whole hostname")
    except ValueError as exc:
        raise ManifestError("Invalid application URL") from exc
    return value


def application_row(name: str, service: str, port: int | None, url: str | None) -> dict:
    """Keep legacy definitions visible, but never synthesize URLs for them."""
    return {"name": name, "url": url} if url is not None else {
        "name": name, "service": service, "port": port,
    }


def _apps(raw: bytes) -> tuple[ApplicationSpec, ...]:
    parsed = _toml(raw)
    if set(parsed) != {"applications"} or not isinstance(parsed["applications"], list):
        raise ManifestError("Project template must contain only [[applications]]")
    result = []
    seen = set()
    for row in parsed["applications"]:
        if not isinstance(row, dict) or set(row) not in (
            {"name", "url"}, {"name", "service"}, {"name", "service", "port"},
        ):
            raise ManifestError("Invalid application entry")
        name = row.get("name")
        if (not isinstance(name, str) or not name or len(name) > 64
                or name != name.strip() or any(ord(c) < 32 for c in name)):
            raise ManifestError("Invalid application label")
        if "url" in row:
            url = validate_application_url(row["url"])
            service, port = "", None
        else:
            # Legacy import compatibility; no automatic link generation.
            url = None
            service = _name(row.get("service"), "Compose service")
            port = row.get("port")
            if port is not None and (type(port) is not int or not 1 <= port <= 65535):
                raise ManifestError("Application port must be 1-65535")
        if name in seen:
            raise ManifestError("Duplicate application")
        seen.add(name)
        result.append(ApplicationSpec(name, service, port, url))
    if len(result) > 30:
        raise ManifestError("Too many applications")
    return tuple(result)


def _tasks(raw: bytes) -> tuple[TaskSpec, ...]:
    parsed = _toml(raw)
    if set(parsed) != {"tasks"} or not isinstance(parsed["tasks"], list):
        raise ManifestError("Task template must contain only [[tasks]]")
    result = []
    seen = set()
    for row in parsed["tasks"]:
        if not isinstance(row, dict) or set(row) - {
            "name", "service", "mode", "image", "network", "command"
        }:
            raise ManifestError("Invalid task entry")
        name = _name(row.get("name"), "task name")
        image = row.get("image", "")
        service = row.get("service", "")
        mode = row.get("mode", "run" if image else "exec")
        network = row.get("network", "none")
        command = row.get("command")
        if (not isinstance(image, str) or len(image) > 255
                or any(c.isspace() for c in image)
                or (image and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]*", image))):
            raise ManifestError("Invalid container image")
        if image and service:
            raise ManifestError("Image tasks cannot also target a Compose service")
        if image:
            if mode != "run":
                raise ManifestError("Standalone image tasks must use run")
        else:
            service = _name(service, "task service")
        if mode not in ("exec", "run") or network not in ("none", "project"):
            raise ManifestError("Invalid task mode or network")
        if not image and network != "none":
            raise ManifestError("Project network option only applies to standalone images")
        if (not isinstance(command, list) or not command or len(command) > 64
                or any(not isinstance(arg, str) or not arg or "\x00" in arg
                       for arg in command)
                or sum(len(arg) for arg in command) > 4096):
            raise ManifestError("Task command must be a bounded argv array")
        if name in seen:
            raise ManifestError("Duplicate task")
        seen.add(name)
        result.append(TaskSpec(name, service, mode, image, network, tuple(command)))
    if len(result) > 40:
        raise ManifestError("Too many tasks")
    return tuple(result)



def _repositories(raw: bytes) -> tuple[RepositorySpec, ...]:
    """Validate optional additional GitHub repositories (never local paths)."""
    parsed = _toml(raw)
    if set(parsed) != {"repositories"} or not isinstance(parsed["repositories"], list):
        raise ManifestError("Repository template must contain only [[repositories]]")
    result = []
    seen_names = set()
    seen_urls = set()
    for row in parsed["repositories"]:
        if not isinstance(row, dict) or set(row) - {"name", "url", "branch"}:
            raise ManifestError("Invalid repository declaration")
        name = _name(row.get("name"), "repository name")
        url = row.get("url")
        if not isinstance(url, str) or not url or url != url.strip():
            raise ManifestError("Invalid repository URL")
        try:
            location = parse_github_url(url)
        except (GitHubSourceError, ValueError) as exc:
            raise ManifestError("Only validated GitHub repository URLs are allowed") from exc
        if location.tree is not None:
            raise ManifestError("Repository declaration must not use GitHub tree URLs")
        branch = row.get("branch", "")
        if (not isinstance(branch, str) or len(branch) > 240
                or (branch and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", branch)
                                or ".." in branch or "//" in branch
                                or branch.endswith(("/", ".", ".lock"))
                                or "@{" in branch
                                or any(part in ("", ".", "..") or part.endswith(".lock")
                                       for part in branch.split("/"))))):
            raise ManifestError("Invalid repository branch")
        if name in seen_names or location.clone_url in seen_urls:
            raise ManifestError("Duplicate repository name or GitHub source")
        seen_names.add(name)
        seen_urls.add(location.clone_url)
        result.append(RepositorySpec(name, location.clone_url, branch))
        if len(result) > 24:
            raise ManifestError("Too many repositories in project manifest")
    return tuple(result)


def parse_template(source: Path) -> ProjectTemplate:
    """Snapshot source declarations; never modify the Git checkout."""
    if source.is_symlink() or not source.is_dir():
        raise ManifestError("Template directory must be a real directory")
    config = source / ".devstack"
    if config.is_symlink() or not config.is_dir():
        raise ManifestError("Template must contain a real .devstack folder")
    app_raw = _read(config, "project.toml", required=True)
    task_raw = _read(config, "tasks.toml")
    compose_raw = _read(config, "compose.yaml")
    repositories_raw = _read(config, "repositories.toml")
    applications = _apps(app_raw)
    tasks = _tasks(task_raw) if task_raw is not None else ()
    files = {"project.toml": app_raw}
    if task_raw is not None:
        files["tasks.toml"] = task_raw
    if compose_raw is not None:
        try:
            compose_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestError("Compose YAML must be UTF-8 text") from exc
        files["compose.yaml"] = compose_raw
    repositories = (_repositories(repositories_raw)
                    if repositories_raw is not None else ())
    return ProjectTemplate(applications, tasks, files, repositories)
