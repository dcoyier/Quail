"""Project configuration, safe paths, atomic publication, and local locks.

This module never opens an index or starts a kernel. Callers own operation-level
lock ordering; publication helpers only perform the file operation they name.
"""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, TracebackType
from urllib.parse import urlsplit

from quail.contracts import (
    JSONObject,
    JSONValue,
    Limits,
    QuailError,
    canonical_json,
    checked_hash,
    digest_bytes,
    embedding_identity,
    json_object,
    json_value,
)

MANIFEST = "quail.toml"
MANAGED_ROOTS = (".quail", "sessions", "warm")
PROVIDER_URLS = {"ollama": "http://127.0.0.1:11434", "openai": "https://api.openai.com/v1"}


def validate_name(name: str, kind: str) -> str:
    """Names are path segments, not paths or silently normalized labels."""
    if not name or name in {".", ".."} or any(char in name for char in ("/", "\\", "\0")):
        raise QuailError(f"Invalid {kind} name: {name!r}", "Use one non-empty path segment")
    json_value(name)
    return name


def _keys(record: JSONObject, allowed: set[str], label: str) -> None:
    unknown = record.keys() - allowed
    if unknown:
        raise QuailError(f"Unknown {label} keys: {', '.join(sorted(unknown))}")


def _text(record: JSONObject, key: str, *, optional: bool = False) -> str | None:
    value = record.get(key)
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise QuailError(f"{key} must be a non-empty string")
    return value


@dataclass(frozen=True)
class EmbeddingConfig:
    embed: str
    revision: str
    dialect: str
    model: str
    base_url: str
    key_env: str | None = None

    @property
    def identity(self) -> str:
        return embedding_identity(self.embed, self.revision)

    def descriptor(self) -> JSONObject:
        return {"id": self.identity, "embed": self.embed, "revision": self.revision}


@dataclass(frozen=True)
class DatasetConfig:
    name: str
    source: Path
    id_column: str | None
    embedding: EmbeddingConfig | None


@dataclass(frozen=True)
class Project:
    root: Path
    datasets: Mapping[str, DatasetConfig]
    limits: Limits

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST

    def path(self, *parts: str) -> Path:
        resolved = self.root.joinpath(*parts).resolve()
        if not resolved.is_relative_to(self.root):
            raise QuailError(f"Project path escapes the study: {resolved}")
        return resolved

    def dataset(self, name: str) -> DatasetConfig:
        try:
            return self.datasets[name]
        except KeyError:
            choices = ", ".join(self.datasets) or "none"
            raise QuailError(
                f"Unknown dataset: {name!r}", f"Available datasets: {choices}"
            ) from None

    def index_path(self, dataset: str) -> Path:
        return self.path(".quail", validate_name(dataset, "dataset") + ".quail")

    def session_path(self, session: str) -> Path:
        return self.path("sessions", validate_name(session, "session"))

    def lock(self, kind: str, name: str = "", *, shared: bool = False) -> FileLock:
        # Hash names only for internal paths; preserve their original spelling everywhere public.
        suffix = digest_bytes(name.encode("utf-8")).removeprefix("sha256:") if name else "project"
        return FileLock(self.path(".quail", "locks", f"{kind}-{suffix}.lock"), shared=shared)

    def session_names(self) -> list[str]:
        directory = self.path("sessions")
        if not directory.exists():
            return []
        return sorted(item.name for item in directory.iterdir() if item.is_dir())

    def check_source(self, source: Path) -> Path:
        path = self.path(str(source))
        protected = (self.manifest, self.root / ".gitignore")
        if any(path == item or same_file(path, item) for item in protected):
            raise QuailError("A source CSV cannot be project metadata")
        if any(path.is_relative_to(self.path(name)) for name in MANAGED_ROOTS):
            raise QuailError("A source CSV cannot be inside a managed Quail directory")
        return path


def discover(start: Path | None = None) -> Project:
    directory = (start or Path.cwd()).resolve()
    for candidate in (directory, *directory.parents):
        if (candidate / MANIFEST).is_file():
            return load(candidate)
    raise QuailError("No quail.toml found", "Enter a study directory or run quail init DIR")


def load(root: Path) -> Project:
    root = root.resolve()
    try:
        source = (root / MANIFEST).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise QuailError(f"Cannot read {root / MANIFEST}: {error}") from error
    return parse(root, source)


def parse(root: Path, source: str) -> Project:
    """Validate a complete prospective manifest before publishing any edit."""
    try:
        document = json_object(json_value(tomllib.loads(source)), "manifest")
    except (ValueError, QuailError) as error:
        raise QuailError(f"Invalid {MANIFEST}: {error}") from error
    _keys(document, {"project", "datasets", "providers", "kernel"}, "manifest")
    project = json_object(document.get("project"), "project")
    _keys(project, {"quail"}, "project")
    if project.get("quail") != "1":
        raise QuailError("Unsupported project schema", 'Expected [project] quail = "1"')
    providers = json_object(document.get("providers", {}), "providers")
    _keys(providers, set(PROVIDER_URLS), "providers")
    resolved_providers = {name: _provider(name, providers.get(name, {})) for name in PROVIDER_URLS}
    limits = Limits.from_record(json_object(document.get("kernel", {}), "kernel"))
    datasets: dict[str, DatasetConfig] = {}
    result = Project(root.resolve(), MappingProxyType(datasets), limits)
    for name, value in json_object(document.get("datasets", {}), "datasets").items():
        validate_name(name, "dataset")
        table = json_object(value, f"dataset {name}")
        _keys(table, {"source", "id", "embed", "embed_revision"}, f"dataset {name}")
        source_name = _text(table, "source")
        assert source_name is not None
        path = result.check_source(result.root / source_name)
        id_column = _text(table, "id", optional=True)
        if id_column is not None and "\0" in id_column:
            raise QuailError("ID column names cannot contain NUL")
        embed = _text(table, "embed", optional=True)
        revision = _text(table, "embed_revision", optional=True)
        embedding = None
        if (embed is None) != (revision is None):
            raise QuailError(f"Dataset {name!r} needs both embed and embed_revision")
        if embed is not None and revision is not None:
            dialect, separator, model = embed.partition("/")
            if not separator or not model or dialect not in resolved_providers:
                raise QuailError("Embedding model must be ollama/MODEL or openai/MODEL")
            url, key_env = resolved_providers[dialect]
            embedding = EmbeddingConfig(embed, revision, dialect, model, url, key_env)
        datasets[name] = DatasetConfig(name, path, id_column, embedding)
    return result


def _provider(name: str, value: JSONValue) -> tuple[str, str | None]:
    table = json_object(value, f"provider {name}")
    _keys(table, {"base_url", "api_key"}, f"provider {name}")
    base_url = table.get("base_url", PROVIDER_URLS[name])
    if not isinstance(base_url, str):
        raise QuailError("Provider base_url must be a URL")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise QuailError("Provider base_url must be an HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise QuailError("Keep provider credentials in environment variables, not URLs")
    credential = _text(table, "api_key", optional=True)
    if credential is None:
        return base_url.rstrip("/"), None
    if not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", credential):
        raise QuailError("Provider api_key must be an env:NAME reference")
    return base_url.rstrip("/"), credential[4:]


def initialize(directory: Path) -> Project:
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    empty = Project(directory, MappingProxyType({}), Limits())
    with empty.lock("project"):
        if empty.manifest.exists():
            raise QuailError(f"Study already exists: {directory}")
        empty.path("sessions").mkdir(exist_ok=True)
        ignore = empty.path(".gitignore")
        previous = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        if ".quail/" not in previous.splitlines():
            separator = "" if not previous or previous.endswith("\n") else "\n"
            atomic_write(ignore, (previous + separator + ".quail/\n").encode("utf-8"))
        atomic_write(empty.manifest, b'[project]\nquail = "1"\n')
    return load(directory)


def registration_text(
    project: Project,
    name: str,
    source: Path,
    *,
    id_column: str | None = None,
    embed: str | None = None,
    revision: str | None = None,
) -> str:
    """Prepare, but do not publish, the one supported manifest append."""
    validate_name(name, "dataset")
    if name in project.datasets:
        raise QuailError(f"Dataset already registered: {name!r}")
    path = project.check_source(source)
    values = {"source": path.relative_to(project.root).as_posix()}
    for key, value in (("id", id_column), ("embed", embed), ("embed_revision", revision)):
        if value is not None:
            values[key] = value
    previous = project.manifest.read_text(encoding="utf-8")
    table = f"\n\n[datasets.{toml_string(name)}]\n"
    table += "".join(f"{key} = {toml_string(value)}\n" for key, value in values.items())
    prospective = previous + table
    parse(project.root, prospective)
    return prospective


def toml_string(value: str) -> str:
    # JSON's basic escapes are TOML-compatible; DEL additionally needs escaping in TOML.
    return canonical_json(value).replace("\x7f", "\\u007f")


@dataclass(frozen=True)
class SessionMetadata:
    name: str
    dataset: str
    source_version: str
    created: str
    id_column: str | None
    forked_from: str | None = None
    description: str | None = None

    def to_toml(self) -> bytes:
        values = {
            "dataset": self.dataset,
            "source_version": self.source_version,
            "created": self.created,
            "id_column": self.id_column,
            "forked_from": self.forked_from,
            "description": self.description,
        }
        return "".join(
            f"{key} = {toml_string(value)}\n" for key, value in values.items() if value is not None
        ).encode("utf-8")


def load_session(project: Project, name: str) -> SessionMetadata:
    path = project.session_path(name) / "session.toml"
    try:
        table = json_object(json_value(tomllib.loads(path.read_text(encoding="utf-8"))))
        _keys(
            table,
            {"dataset", "source_version", "created", "id_column", "forked_from", "description"},
            "session",
        )
        dataset = _text(table, "dataset")
        created = _text(table, "created")
        assert dataset is not None and created is not None
        validate_name(dataset, "dataset")
        return SessionMetadata(
            name=name,
            dataset=dataset,
            source_version=checked_hash(table.get("source_version"), "session source version"),
            created=created,
            id_column=_text(table, "id_column", optional=True),
            forked_from=_text(table, "forked_from", optional=True),
            description=_text(table, "description", optional=True),
        )
    except (OSError, ValueError, QuailError) as error:
        raise QuailError(f"Cannot read session {name!r}: {error}") from error


def same_file(first: Path, second: Path) -> bool:
    try:
        return first.samefile(second)
    except FileNotFoundError:
        return False


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: bytes) -> None:
    """Publish complete bytes, syncing the file and its containing directory.

    The caller validates the destination and holds its metadata lock. A failure
    after replacement is deliberately reported: durability may be uncertain.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


class FileLock:
    """A single advisory lock, retained by its owner until explicitly closed."""

    def __init__(self, path: Path, *, shared: bool = False) -> None:
        self.path = path
        self.shared = shared
        self._descriptor: int | None = None

    def acquire(self) -> FileLock:
        if self._descriptor is not None:
            raise RuntimeError("Lock is already acquired by this handle")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            operation = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise QuailError(
                "Quail resource is busy", "Finish or close its active owner"
            ) from error
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def close(self) -> None:
        if self._descriptor is not None:
            descriptor, self._descriptor = self._descriptor, None
            os.close(descriptor)

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
