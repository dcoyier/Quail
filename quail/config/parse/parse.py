"""Parse and validate slim hand-edited quail.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from quail.config.errors import ConfigError
from quail.config.models import (
    DatasetSpec,
    EmbeddingProfile,
    OllamaProvider,
    OpenRouterProvider,
    ProvidersConfig,
    QuailConfig,
    SearchWarmConfig,
    UserSpec,
    WorkspaceSpec,
)

_ALLOWED_ROOT_UNRESTRICTED = frozenset(
    {"core", "auth", "hosting", "datasets", "providers", "search"}
)
_ALLOWED_ROOT_CLERK = frozenset(
    {"core", "auth", "hosting", "workspaces", "users", "providers", "search"}
)
_ALLOWED_CORE = frozenset({"database", "feedback", "search_database"})
_ALLOWED_AUTH_UNRESTRICTED = frozenset({"mode", "workspace"})
_ALLOWED_AUTH_CLERK = frozenset({"mode", "clerk_domain"})
_ALLOWED_HOSTING = frozenset({"bind", "port"})
_ALLOWED_DATASET = frozenset({"id", "source", "name", "embedding"})
_ALLOWED_EMBEDDING = frozenset({"provider", "model", "dimensions", "revision"})
_ALLOWED_WORKSPACE = frozenset({"id", "datasets"})
_ALLOWED_USER = frozenset(
    {"id", "clerk_user_id", "workspaces", "default_workspace", "lock_workspace"}
)
_ALLOWED_PROVIDERS = frozenset({"ollama", "openrouter"})
_ALLOWED_OLLAMA = frozenset({"base_url"})
_ALLOWED_OPENROUTER = frozenset({"base_url", "api_key"})
_ALLOWED_SEARCH = frozenset({"warm"})
_ALLOWED_SEARCH_WARM = frozenset({"embed_batch_size", "max_concurrent_embed_requests"})
_DEFAULT_EMBED_BATCH_SIZE = 32
_DEFAULT_MAX_CONCURRENT_EMBED_REQUESTS = 2
_MIN_EMBED_BATCH_SIZE = 1
_MAX_EMBED_BATCH_SIZE = 512
_MIN_CONCURRENT_EMBED_REQUESTS = 1
_MAX_CONCURRENT_EMBED_REQUESTS = 32


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
    search_database: Path | None = None
    if "search_database" in core:
        search_database = _resolve_path(
            _require_string(core, "search_database", label="core.search_database"),
            base=manifest_path.parent,
        )
        if search_database == database:
            raise ConfigError("core.search_database must be distinct from core.database")

    hosting = _require_table(data, "hosting")
    _reject_unknown(hosting, _ALLOWED_HOSTING, label="hosting")
    bind = _require_string(hosting, "bind", label="hosting.bind")
    port = hosting.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ConfigError("hosting.port must be an integer from 1 to 65535")

    providers = _parse_providers(data.get("providers"))
    search_warm = _parse_search_warm(data.get("search"))

    if mode == "unrestricted":
        config = _parse_unrestricted(
            data,
            auth=auth,
            manifest_path=manifest_path,
            database=database,
            feedback=feedback,
            search_database=search_database,
            providers=providers,
            search_warm=search_warm,
            bind=bind,
            port=port,
        )
    else:
        config = _parse_clerk(
            data,
            auth=auth,
            manifest_path=manifest_path,
            database=database,
            feedback=feedback,
            search_database=search_database,
            providers=providers,
            search_warm=search_warm,
            bind=bind,
            port=port,
        )
    _validate_embedding_wiring(config)
    return config


def _parse_unrestricted(
    data: dict[str, Any],
    *,
    auth: dict[str, Any],
    manifest_path: Path,
    database: Path,
    feedback: Path,
    search_database: Path | None,
    providers: ProvidersConfig,
    search_warm: SearchWarmConfig,
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
        search_database=search_database,
        providers=providers,
        search_warm=search_warm,
    )


def _parse_clerk(
    data: dict[str, Any],
    *,
    auth: dict[str, Any],
    manifest_path: Path,
    database: Path,
    feedback: Path,
    search_database: Path | None,
    providers: ProvidersConfig,
    search_warm: SearchWarmConfig,
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
        search_database=search_database,
        providers=providers,
        search_warm=search_warm,
    )


def _parse_search_warm(raw: object) -> SearchWarmConfig:
    if raw is None:
        return SearchWarmConfig()
    if not isinstance(raw, dict):
        raise ConfigError("search must be a table")
    _reject_unknown(raw, _ALLOWED_SEARCH, label="search")
    warm_raw = raw.get("warm")
    if warm_raw is None:
        return SearchWarmConfig()
    if not isinstance(warm_raw, dict):
        raise ConfigError("search.warm must be a table")
    _reject_unknown(warm_raw, _ALLOWED_SEARCH_WARM, label="search.warm")
    batch = _DEFAULT_EMBED_BATCH_SIZE
    if "embed_batch_size" in warm_raw:
        batch = warm_raw["embed_batch_size"]
        if (
            not isinstance(batch, int)
            or isinstance(batch, bool)
            or not (_MIN_EMBED_BATCH_SIZE <= batch <= _MAX_EMBED_BATCH_SIZE)
        ):
            raise ConfigError(
                "search.warm.embed_batch_size must be an integer from "
                f"{_MIN_EMBED_BATCH_SIZE} to {_MAX_EMBED_BATCH_SIZE}"
            )
    concurrency = _DEFAULT_MAX_CONCURRENT_EMBED_REQUESTS
    if "max_concurrent_embed_requests" in warm_raw:
        concurrency = warm_raw["max_concurrent_embed_requests"]
        if (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or not (_MIN_CONCURRENT_EMBED_REQUESTS <= concurrency <= _MAX_CONCURRENT_EMBED_REQUESTS)
        ):
            raise ConfigError(
                "search.warm.max_concurrent_embed_requests must be an integer from "
                f"{_MIN_CONCURRENT_EMBED_REQUESTS} to {_MAX_CONCURRENT_EMBED_REQUESTS}"
            )
    return SearchWarmConfig(
        embed_batch_size=batch,
        max_concurrent_embed_requests=concurrency,
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
        embedding: EmbeddingProfile | None = None
        if "embedding" in entry:
            embedding = _parse_embedding(entry["embedding"], label=f"{label}.embedding")
        datasets.append(
            DatasetSpec(
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                source=source,
                name=name,
                embedding=embedding,
            )
        )
    return tuple(datasets)


def _parse_embedding(raw: object, *, label: str) -> EmbeddingProfile:
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be a table")
    _reject_unknown(raw, _ALLOWED_EMBEDDING, label=label)
    provider = _require_string(raw, "provider", label=f"{label}.provider")
    if provider not in {"ollama", "openrouter"}:
        raise ConfigError(f'{label}.provider must be "ollama" or "openrouter"')
    model = _require_string(raw, "model", label=f"{label}.model")
    dimensions = raw.get("dimensions")
    if not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0:
        raise ConfigError(f"{label}.dimensions must be an integer greater than 0")
    revision = _require_string(raw, "revision", label=f"{label}.revision")
    return EmbeddingProfile(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        dimensions=dimensions,
        revision=revision,
    )


def _parse_providers(raw: object) -> ProvidersConfig:
    if raw is None:
        return ProvidersConfig()
    if not isinstance(raw, dict):
        raise ConfigError("[providers] must be a table")
    _reject_unknown(raw, _ALLOWED_PROVIDERS, label="providers")
    ollama: OllamaProvider | None = None
    openrouter: OpenRouterProvider | None = None
    if "ollama" in raw:
        block = raw["ollama"]
        if not isinstance(block, dict):
            raise ConfigError("[providers.ollama] must be a table")
        _reject_unknown(block, _ALLOWED_OLLAMA, label="providers.ollama")
        base_url = _require_string(block, "base_url", label="providers.ollama.base_url")
        ollama = OllamaProvider(base_url=_normalize_base_url(base_url))
    if "openrouter" in raw:
        block = raw["openrouter"]
        if not isinstance(block, dict):
            raise ConfigError("[providers.openrouter] must be a table")
        _reject_unknown(block, _ALLOWED_OPENROUTER, label="providers.openrouter")
        base_url = _require_string(block, "base_url", label="providers.openrouter.base_url")
        api_key = _require_string(block, "api_key", label="providers.openrouter.api_key")
        if not api_key.startswith("env:"):
            raise ConfigError(
                "providers.openrouter.api_key must be an env:NAME reference (not a raw secret)"
            )
        openrouter = OpenRouterProvider(
            base_url=_normalize_base_url(base_url),
            api_key=api_key,
        )
    return ProvidersConfig(ollama=ollama, openrouter=openrouter)


def _validate_embedding_wiring(config: QuailConfig) -> None:
    needed_providers: set[str] = set()
    for spec in config.datasets:
        if spec.embedding is None:
            continue
        needed_providers.add(spec.embedding.provider)
    if not needed_providers:
        return
    if config.search_database is None:
        raise ConfigError(
            "core.search_database is required when any dataset declares [datasets.embedding]"
        )
    if "ollama" in needed_providers and config.providers.ollama is None:
        raise ConfigError("[providers.ollama] is required for datasets using provider ollama")
    if "openrouter" in needed_providers and config.providers.openrouter is None:
        raise ConfigError(
            "[providers.openrouter] is required for datasets using provider openrouter"
        )


def _normalize_base_url(value: str) -> str:
    return value.rstrip("/")


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
