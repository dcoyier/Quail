"""Project directory: load quail.toml and resolve paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

_SCHEMA = "1"
_ROOT_KEYS = {"project", "datasets", "providers", "kernel"}
_PROJECT_KEYS = {"quail"}
_DATASET_KEYS = {"source", "id", "embed"}
_PROVIDER_KEYS = {"base_url", "api_key"}
_KERNEL_KEYS = {
    "cpu_seconds",
    "wall_seconds",
    "memory_mb",
    "max_limit",
    "output_kib",
}

_OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"
_OPENAI_DEFAULT_URL = "https://api.openai.com/v1"


class QuailError(Exception):
    """A failure the caller can fix. Kernel and host share this name."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass(frozen=True, slots=True)
class KernelLimits:
    cpu_seconds: int = 30
    wall_seconds: int = 120
    memory_mb: int = 1024
    max_limit: int = 1000
    output_kib: int = 64


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    name: str
    source: Path
    id_column: str | None = None
    embed: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    base_url: str
    api_key_env: str | None = None


@dataclass(frozen=True, slots=True)
class Manifest:
    schema: str
    datasets: dict[str, DatasetSpec]
    providers: dict[str, ProviderSpec]
    kernel: KernelLimits


class Project:
    """A directory that contains quail.toml. Hosted's handle is Project(path)."""

    def __init__(self, path: str | Path) -> None:
        given = Path(path).expanduser()
        if given.is_file():
            if given.name != "quail.toml":
                raise QuailError(f"not a quail.toml: {given}")
            root = given.parent
        else:
            root = given
        self.root = root.resolve()
        self.manifest_path = self.root / "quail.toml"
        if not self.manifest_path.is_file():
            raise QuailError(
                f"no quail.toml in {self.root}",
                hint="run quail init, or point at a project directory",
            )
        self.manifest = load_manifest(self.manifest_path)

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def index_dir(self) -> Path:
        return self.root / ".quail"

    def index_path(self, dataset: str) -> Path:
        self._dataset(dataset)
        return self.index_dir / f"{dataset}.quail"

    def session_dir(self, name: str) -> Path:
        return self.sessions_dir / name

    def session_manifest_path(self, name: str) -> Path:
        return self.session_dir(name) / "session.toml"

    def session_log_dir(self, name: str) -> Path:
        return self.session_dir(name) / "log"

    def dataset_source(self, dataset: str) -> Path:
        return self._dataset(dataset).source

    def _dataset(self, dataset: str) -> DatasetSpec:
        spec = self.manifest.datasets.get(dataset)
        if spec is None:
            known = ", ".join(sorted(self.manifest.datasets)) or "(none)"
            raise QuailError(f"unknown dataset {dataset!r}", hint=f"known: {known}")
        return spec


def load_manifest(path: str | Path) -> Manifest:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise QuailError(f"no quail.toml at {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise QuailError(f"invalid TOML in {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise QuailError("manifest must be a table")
    _unknown("quail.toml", raw, _ROOT_KEYS)
    root = path.parent
    return Manifest(
        schema=_parse_schema(raw.get("project")),
        datasets=_parse_datasets(raw.get("datasets"), root),
        providers=_parse_providers(raw.get("providers")),
        kernel=_parse_kernel(raw.get("kernel")),
    )


def _parse_schema(raw: object) -> str:
    if raw is None:
        raise QuailError("missing [project]", hint='add [project] with quail = "1"')
    table = _table("project", raw)
    _unknown("project", table, _PROJECT_KEYS)
    if "quail" not in table:
        raise QuailError('missing project.quail', hint='set quail = "1"')
    value = table["quail"]
    if not isinstance(value, str):
        raise QuailError('project.quail must be a string', hint='write quail = "1"')
    if value != _SCHEMA:
        raise QuailError(f"unsupported manifest version {value!r}")
    return value


def _parse_datasets(raw: object, root: Path) -> dict[str, DatasetSpec]:
    if raw is None:
        return {}
    table = _table("datasets", raw)
    datasets: dict[str, DatasetSpec] = {}
    for name, spec in table.items():
        section = f"datasets.{name}"
        body = _table(section, spec)
        _unknown(section, body, _DATASET_KEYS)
        if "source" not in body:
            raise QuailError(f"missing {section}.source")
        source = _str(f"{section}.source", body["source"])
        if not source:
            raise QuailError(f"{section}.source is empty")
        id_column = _optional_str(f"{section}.id", body.get("id"))
        embed = _optional_str(f"{section}.embed", body.get("embed"))
        datasets[name] = DatasetSpec(
            name=name,
            source=(root / source).resolve(),
            id_column=id_column or None,
            embed=embed or None,
        )
    return datasets


def _parse_providers(raw: object) -> dict[str, ProviderSpec]:
    if raw is None:
        return {}
    table = _table("providers", raw)
    providers: dict[str, ProviderSpec] = {}
    for name, spec in table.items():
        section = f"providers.{name}"
        body = _table(section, spec)
        _unknown(section, body, _PROVIDER_KEYS)
        default_url = _default_provider_url(name)
        if "base_url" in body:
            base_url = _str(f"{section}.base_url", body["base_url"])
        elif default_url is not None:
            base_url = default_url
        else:
            raise QuailError(f"missing {section}.base_url")
        if not base_url:
            raise QuailError(f"{section}.base_url is empty")
        api_key_env = None
        if "api_key" in body:
            api_key_env = _env_name(f"{section}.api_key", body["api_key"])
        providers[name] = ProviderSpec(
            name=name,
            base_url=base_url,
            api_key_env=api_key_env,
        )
    return providers


def _parse_kernel(raw: object) -> KernelLimits:
    if raw is None:
        return KernelLimits()
    table = _table("kernel", raw)
    _unknown("kernel", table, _KERNEL_KEYS)
    values: dict[str, int] = {}
    for key in _KERNEL_KEYS:
        if key in table:
            values[key] = _positive_int(f"kernel.{key}", table[key])
    return KernelLimits(**values)


def _default_provider_url(name: str) -> str | None:
    if name == "ollama":
        return _OLLAMA_DEFAULT_URL
    if name == "openai":
        return _OPENAI_DEFAULT_URL
    return None


def _table(section: str, raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise QuailError(f"{section} must be a table")
    return raw


def _unknown(section: str, table: dict[str, object], allowed: set[str]) -> None:
    extra = [key for key in table if key not in allowed]
    if extra:
        raise QuailError(f"unknown key {extra[0]!r} in {section}")


def _str(where: str, raw: object) -> str:
    if not isinstance(raw, str):
        raise QuailError(f"{where} must be a string")
    return raw


def _optional_str(where: str, raw: object) -> str | None:
    if raw is None:
        return None
    return _str(where, raw)


def _positive_int(where: str, raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise QuailError(f"{where} must be an integer")
    if raw <= 0:
        raise QuailError(f"{where} must be positive")
    return raw


def _env_name(where: str, raw: object) -> str:
    value = _str(where, raw)
    if not value.startswith("env:") or len(value) == 4:
        raise QuailError(
            f"{where} must be env:NAME, not a literal",
            hint='write api_key = "env:OPENAI_API_KEY"',
        )
    name = value[4:]
    if not name.isascii() or not name.isidentifier():
        raise QuailError(f"{where} is not a valid environment variable name")
    return name
