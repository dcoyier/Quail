"""Parse and validate slim hand-edited quail.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from quail.config.errors import ConfigError
from quail.config.models import DatasetSpec, QuailConfig

_ALLOWED_ROOT = frozenset({"core", "auth", "hosting", "datasets"})
_ALLOWED_CORE = frozenset({"database", "feedback"})
_ALLOWED_AUTH = frozenset({"mode", "workspace"})
_ALLOWED_HOSTING = frozenset({"bind", "port"})
_ALLOWED_DATASET = frozenset({"id", "source", "name"})


def load_config(config_path: str | Path) -> QuailConfig:
    """Load slim quail.toml from an absolute path; resolve relatives to its dir."""

    path = Path(config_path)
    if not path.is_absolute():
        raise ConfigError("--config must be an absolute path")
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"Cannot read config file: {path}") from error
    return parse_config(raw_text, manifest_path=path)


def parse_config(raw_text: str, *, manifest_path: Path) -> QuailConfig:
    """Parse slim TOML text; resolve paths against the manifest directory."""

    if not manifest_path.is_absolute():
        raise ConfigError("manifest_path must be absolute")
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid TOML: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError("quail.toml root must be a table")
    _reject_unknown(data, _ALLOWED_ROOT, label="root")

    core = _require_table(data, "core")
    _reject_unknown(core, _ALLOWED_CORE, label="core")
    database = _resolve_path(
        _require_string(core, "database", label="core.database"),
        base=manifest_path.parent,
    )
    feedback = _resolve_path(
        _require_string(core, "feedback", label="core.feedback"),
        base=manifest_path.parent,
    )

    auth = _require_table(data, "auth")
    _reject_unknown(auth, _ALLOWED_AUTH, label="auth")
    mode = _require_string(auth, "mode", label="auth.mode")
    if mode != "unrestricted":
        raise ConfigError('auth.mode must be "unrestricted" in this slice')
    workspace_id = _require_string(auth, "workspace", label="auth.workspace")

    hosting = _require_table(data, "hosting")
    _reject_unknown(hosting, _ALLOWED_HOSTING, label="hosting")
    bind = _require_string(hosting, "bind", label="hosting.bind")
    port = hosting.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ConfigError("hosting.port must be an integer from 1 to 65535")

    datasets_raw = data.get("datasets", [])
    if not isinstance(datasets_raw, list):
        raise ConfigError("datasets must be an array of tables")
    datasets: list[DatasetSpec] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(datasets_raw):
        label = f"datasets[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be a table")
        _reject_unknown(entry, _ALLOWED_DATASET, label=label)
        dataset_id = _require_string(entry, "id", label=f"{label}.id")
        if dataset_id in seen_ids:
            raise ConfigError(f"Duplicate dataset id: {dataset_id}")
        seen_ids.add(dataset_id)
        source = _resolve_path(
            _require_string(entry, "source", label=f"{label}.source"),
            base=manifest_path.parent,
        )
        name: str | None = None
        if "name" in entry:
            name = _require_string(entry, "name", label=f"{label}.name")
        datasets.append(DatasetSpec(dataset_id=dataset_id, source=source, name=name))

    return QuailConfig(
        manifest_path=manifest_path,
        database=database,
        feedback=feedback,
        workspace_id=workspace_id,
        bind=bind,
        port=port,
        datasets=tuple(datasets),
    )


def _require_table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"Missing or invalid [{key}] table")
    return value


def _require_string(data: dict[str, Any], key: str, *, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value.strip()


def _reject_unknown(data: dict[str, Any], allowed: frozenset[str], *, label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"Unknown {label} key(s): {', '.join(unknown)}")


def _resolve_path(value: str, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()
