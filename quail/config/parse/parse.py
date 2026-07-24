"""Parse and validate slim hand-edited quail.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from quail.config.errors import ConfigError
from quail.config.models import DatasetSpec, QuailConfig, UserSpec, WorkspaceSpec

_ALLOWED_ROOT_UNRESTRICTED = frozenset({"core", "auth", "hosting", "datasets"})
_ALLOWED_ROOT_CLERK = frozenset({"core", "auth", "hosting", "workspaces", "users"})
_ALLOWED_CORE = frozenset({"database", "feedback"})
_ALLOWED_AUTH_UNRESTRICTED = frozenset({"mode", "workspace"})
_ALLOWED_AUTH_CLERK = frozenset({"mode", "clerk_domain"})
_ALLOWED_HOSTING = frozenset({"bind", "port"})
_ALLOWED_DATASET = frozenset({"id", "source", "name"})
_ALLOWED_WORKSPACE = frozenset({"id", "datasets"})
_ALLOWED_USER = frozenset(
    {"id", "clerk_user_id", "workspaces", "default_workspace", "lock_workspace"}
)


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

    auth = _require_table(data, "auth")
    mode = _require_string(auth, "mode", label="auth.mode")
    if mode not in {"unrestricted", "clerk"}:
        raise ConfigError('auth.mode must be "unrestricted" or "clerk"')

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

    hosting = _require_table(data, "hosting")
    _reject_unknown(hosting, _ALLOWED_HOSTING, label="hosting")
    bind = _require_string(hosting, "bind", label="hosting.bind")
    port = hosting.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ConfigError("hosting.port must be an integer from 1 to 65535")

    if mode == "unrestricted":
        return _parse_unrestricted(
            data,
            auth=auth,
            manifest_path=manifest_path,
            database=database,
            feedback=feedback,
            bind=bind,
            port=port,
        )
    return _parse_clerk(
        data,
        auth=auth,
        manifest_path=manifest_path,
        database=database,
        feedback=feedback,
        bind=bind,
        port=port,
    )


def _parse_unrestricted(
    data: dict[str, Any],
    *,
    auth: dict[str, Any],
    manifest_path: Path,
    database: Path,
    feedback: Path,
    bind: str,
    port: int,
) -> QuailConfig:
    _reject_unknown(data, _ALLOWED_ROOT_UNRESTRICTED, label="root")
    _reject_unknown(auth, _ALLOWED_AUTH_UNRESTRICTED, label="auth")
    workspace_id = _require_string(auth, "workspace", label="auth.workspace")
    datasets = _parse_flat_datasets(
        data.get("datasets", []),
        workspace_id=workspace_id,
        base=manifest_path.parent,
    )
    workspace = WorkspaceSpec(workspace_id=workspace_id, datasets=datasets)
    return QuailConfig(
        manifest_path=manifest_path,
        database=database,
        feedback=feedback,
        auth_mode="unrestricted",
        bind=bind,
        port=port,
        workspace_id=workspace_id,
        clerk_domain=None,
        workspaces=(workspace,),
        users=(),
        datasets=datasets,
    )


def _parse_clerk(
    data: dict[str, Any],
    *,
    auth: dict[str, Any],
    manifest_path: Path,
    database: Path,
    feedback: Path,
    bind: str,
    port: int,
) -> QuailConfig:
    _reject_unknown(data, _ALLOWED_ROOT_CLERK, label="root")
    _reject_unknown(auth, _ALLOWED_AUTH_CLERK, label="auth")
    if "workspace" in auth:
        raise ConfigError("auth.workspace is not allowed when auth.mode is clerk")
    if "datasets" in data:
        raise ConfigError("root [[datasets]] is not allowed when auth.mode is clerk")
    clerk_domain = _require_string(auth, "clerk_domain", label="auth.clerk_domain")

    workspaces_raw = data.get("workspaces")
    if not isinstance(workspaces_raw, list) or not workspaces_raw:
        raise ConfigError("clerk mode requires non-empty [[workspaces]]")
    workspaces: list[WorkspaceSpec] = []
    seen_workspace_ids: set[str] = set()
    flat_datasets: list[DatasetSpec] = []
    for index, entry in enumerate(workspaces_raw):
        label = f"workspaces[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be a table")
        _reject_unknown(entry, _ALLOWED_WORKSPACE, label=label)
        workspace_id = _require_string(entry, "id", label=f"{label}.id")
        if workspace_id in seen_workspace_ids:
            raise ConfigError(f"Duplicate workspace id: {workspace_id}")
        seen_workspace_ids.add(workspace_id)
        nested = entry.get("datasets", [])
        datasets = _parse_flat_datasets(
            nested,
            workspace_id=workspace_id,
            base=manifest_path.parent,
            label_prefix=f"{label}.datasets",
        )
        # Dataset ids unique within a workspace (enforced above); also unique
        # across deployment for simpler ops in this slice.
        for spec in datasets:
            if any(existing.dataset_id == spec.dataset_id for existing in flat_datasets):
                raise ConfigError(f"Duplicate dataset id: {spec.dataset_id}")
        flat_datasets.extend(datasets)
        workspaces.append(WorkspaceSpec(workspace_id=workspace_id, datasets=datasets))

    users_raw = data.get("users")
    if not isinstance(users_raw, list) or not users_raw:
        raise ConfigError("clerk mode requires non-empty [[users]]")
    users: list[UserSpec] = []
    seen_user_ids: set[str] = set()
    seen_clerk_ids: set[str] = set()
    for index, entry in enumerate(users_raw):
        label = f"users[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be a table")
        _reject_unknown(entry, _ALLOWED_USER, label=label)
        user_id = _require_string(entry, "id", label=f"{label}.id")
        if user_id in seen_user_ids:
            raise ConfigError(f"Duplicate user id: {user_id}")
        seen_user_ids.add(user_id)
        clerk_user_id = _require_string(entry, "clerk_user_id", label=f"{label}.clerk_user_id")
        if clerk_user_id in seen_clerk_ids:
            raise ConfigError(f"Duplicate clerk_user_id: {clerk_user_id}")
        seen_clerk_ids.add(clerk_user_id)
        memberships = entry.get("workspaces")
        if not isinstance(memberships, list) or not memberships:
            raise ConfigError(f"{label}.workspaces must be a non-empty array of strings")
        workspace_ids: list[str] = []
        for mid, member in enumerate(memberships):
            if not isinstance(member, str) or not member.strip():
                raise ConfigError(f"{label}.workspaces[{mid}] must be a non-empty string")
            member_id = member.strip()
            if member_id not in seen_workspace_ids:
                raise ConfigError(f"{label}.workspaces references unknown workspace: {member_id}")
            if member_id in workspace_ids:
                raise ConfigError(f"{label}.workspaces duplicates {member_id}")
            workspace_ids.append(member_id)
        default_workspace: str | None = None
        if "default_workspace" in entry:
            default_workspace = _require_string(
                entry, "default_workspace", label=f"{label}.default_workspace"
            )
            if default_workspace not in workspace_ids:
                raise ConfigError(f"{label}.default_workspace must be one of the user's workspaces")
        lock_workspace = False
        if "lock_workspace" in entry:
            raw_lock = entry["lock_workspace"]
            if not isinstance(raw_lock, bool):
                raise ConfigError(f"{label}.lock_workspace must be a boolean")
            lock_workspace = raw_lock
            if lock_workspace and default_workspace is None:
                raise ConfigError(f"{label}.lock_workspace requires default_workspace to be set")
        users.append(
            UserSpec(
                user_id=user_id,
                clerk_user_id=clerk_user_id,
                workspaces=tuple(workspace_ids),
                default_workspace=default_workspace,
                lock_workspace=lock_workspace,
            )
        )

    return QuailConfig(
        manifest_path=manifest_path,
        database=database,
        feedback=feedback,
        auth_mode="clerk",
        bind=bind,
        port=port,
        workspace_id=None,
        clerk_domain=clerk_domain,
        workspaces=tuple(workspaces),
        users=tuple(users),
        datasets=tuple(flat_datasets),
    )


def _parse_flat_datasets(
    raw: object,
    *,
    workspace_id: str,
    base: Path,
    label_prefix: str = "datasets",
) -> tuple[DatasetSpec, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"{label_prefix} must be an array of tables")
    datasets: list[DatasetSpec] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw):
        label = f"{label_prefix}[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be a table")
        _reject_unknown(entry, _ALLOWED_DATASET, label=label)
        dataset_id = _require_string(entry, "id", label=f"{label}.id")
        if dataset_id in seen_ids:
            raise ConfigError(f"Duplicate dataset id: {dataset_id}")
        seen_ids.add(dataset_id)
        source = _resolve_path(
            _require_string(entry, "source", label=f"{label}.source"),
            base=base,
        )
        name: str | None = None
        if "name" in entry:
            name = _require_string(entry, "name", label=f"{label}.name")
        datasets.append(
            DatasetSpec(
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                source=source,
                name=name,
            )
        )
    return tuple(datasets)


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
