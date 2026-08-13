"""Parse and validate slim hand-edited quail.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from quail.config.errors import ConfigError
from quail.config.models import (
    ConnectorBinding,
    DatasetSpec,
    EmbeddingProfile,
    ExtensionPin,
    OllamaProvider,
    OpenRouterProvider,
    ProvidersConfig,
    QuailConfig,
    SearchWarmConfig,
    UserSpec,
    WorkspaceSpec,
)

_ALLOWED_ROOT_UNRESTRICTED = frozenset(
    {"core", "auth", "hosting", "datasets", "providers", "search", "extensions", "connectors"}
)
_ALLOWED_ROOT_CLERK = frozenset(
    {
        "core",
        "auth",
        "hosting",
        "workspaces",
        "users",
        "providers",
        "search",
        "extensions",
    }
)
_ALLOWED_CORE = frozenset({"database", "feedback", "search_database"})
_ALLOWED_AUTH_UNRESTRICTED = frozenset({"mode", "workspace"})
_ALLOWED_AUTH_CLERK = frozenset({"mode", "clerk_domain", "clerk_authorized_parties"})
_ALLOWED_HOSTING = frozenset(
    {
        "bind",
        "port",
        "max_concurrent_executions",
        "public_base_url",
        "allow_public_unrestricted",
        "allow_insecure_http",
        "include_dataset_docs_in_setup",
    }
)
_DEFAULT_MAX_CONCURRENT_EXECUTIONS = 2
_MIN_MAX_CONCURRENT_EXECUTIONS = 1
_MAX_MAX_CONCURRENT_EXECUTIONS = 100
_ALLOWED_DATASET = frozenset({"id", "source", "name", "embedding", "lexical"})
_ALLOWED_EMBEDDING = frozenset({"provider", "model", "dimensions", "revision", "fields"})
_ALLOWED_LEXICAL = frozenset({"fields"})
_ALLOWED_WORKSPACE = frozenset({"id", "datasets", "connectors"})
_ALLOWED_USER = frozenset(
    {"id", "clerk_user_id", "workspaces", "default_workspace", "lock_workspace"}
)
_ALLOWED_PROVIDERS = frozenset({"ollama", "openrouter"})
_ALLOWED_OLLAMA = frozenset({"base_url"})
_ALLOWED_OPENROUTER = frozenset({"base_url", "api_key"})
_ALLOWED_SEARCH = frozenset({"warm"})
_ALLOWED_SEARCH_WARM = frozenset({"embed_batch_size", "max_concurrent_embed_requests"})
_ALLOWED_EXTENSION = frozenset({"id", "version"})
_ALLOWED_CONNECTOR = frozenset({"id", "config", "datasets"})
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
    max_concurrent_executions = _parse_max_concurrent_executions(hosting)
    public_base_url = _parse_public_base_url(hosting, bind=bind, port=port)
    allow_public_unrestricted = _parse_allow_public_unrestricted(hosting)
    allow_insecure_http = _parse_allow_insecure_http(hosting)
    include_dataset_docs_in_setup = _parse_include_dataset_docs_in_setup(hosting)

    providers = _parse_providers(data.get("providers"))
    search_warm = _parse_search_warm(data.get("search"))

    if mode == "unrestricted":
        if allow_insecure_http:
            raise ConfigError(
                "hosting.allow_insecure_http is only valid when auth.mode is clerk"
            )
        _require_unrestricted_loopback(
            bind=bind,
            public_base_url=public_base_url,
            allow_public_unrestricted=allow_public_unrestricted,
        )
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
            public_base_url=public_base_url,
            max_concurrent_executions=max_concurrent_executions,
            allow_public_unrestricted=allow_public_unrestricted,
            include_dataset_docs_in_setup=include_dataset_docs_in_setup,
        )
    else:
        if allow_public_unrestricted:
            raise ConfigError(
                "hosting.allow_public_unrestricted is only valid when auth.mode is unrestricted"
            )
        _require_clerk_https_public_base_url(
            public_base_url=public_base_url,
            allow_insecure_http=allow_insecure_http,
        )
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
            public_base_url=public_base_url,
            max_concurrent_executions=max_concurrent_executions,
            allow_insecure_http=allow_insecure_http,
            include_dataset_docs_in_setup=include_dataset_docs_in_setup,
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
    public_base_url: str,
    max_concurrent_executions: int,
    allow_public_unrestricted: bool,
    include_dataset_docs_in_setup: bool,
) -> QuailConfig:
    _reject_unknown(data, _ALLOWED_ROOT_UNRESTRICTED, label="root")
    _reject_unknown(auth, _ALLOWED_AUTH_UNRESTRICTED, label="auth")
    workspace_id = _require_string(auth, "workspace", label="auth.workspace")
    datasets = _parse_flat_datasets(
        data.get("datasets", []),
        workspace_id=workspace_id,
        base=manifest_path.parent,
    )
    extensions = _parse_extensions(data.get("extensions", []))
    connectors = _parse_connectors(
        data.get("connectors", []),
        extension_ids={pin.extension_id for pin in extensions},
        dataset_ids={spec.dataset_id for spec in datasets},
        label_prefix="connectors",
    )
    workspace = WorkspaceSpec(
        workspace_id=workspace_id,
        datasets=datasets,
        connectors=connectors,
    )
    return QuailConfig(
        manifest_path=manifest_path,
        database=database,
        feedback=feedback,
        auth_mode="unrestricted",
        bind=bind,
        port=port,
        public_base_url=public_base_url,
        workspace_id=workspace_id,
        clerk_domain=None,
        clerk_authorized_parties=(),
        workspaces=(workspace,),
        users=(),
        datasets=datasets,
        search_database=search_database,
        providers=providers,
        search_warm=search_warm,
        max_concurrent_executions=max_concurrent_executions,
        extensions=extensions,
        allow_public_unrestricted=allow_public_unrestricted,
        include_dataset_docs_in_setup=include_dataset_docs_in_setup,
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
    public_base_url: str,
    max_concurrent_executions: int,
    allow_insecure_http: bool,
    include_dataset_docs_in_setup: bool,
) -> QuailConfig:
    _reject_unknown(data, _ALLOWED_ROOT_CLERK, label="root")
    _reject_unknown(auth, _ALLOWED_AUTH_CLERK, label="auth")
    if "workspace" in auth:
        raise ConfigError("auth.workspace is not allowed when auth.mode is clerk")
    if "datasets" in data:
        raise ConfigError("root [[datasets]] is not allowed when auth.mode is clerk")
    clerk_domain = _require_string(auth, "clerk_domain", label="auth.clerk_domain")
    clerk_authorized_parties = _parse_clerk_authorized_parties(auth)
    extensions = _parse_extensions(data.get("extensions", []))
    extension_ids = {pin.extension_id for pin in extensions}

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
        connectors = _parse_connectors(
            entry.get("connectors", []),
            extension_ids=extension_ids,
            dataset_ids={spec.dataset_id for spec in datasets},
            label_prefix=f"{label}.connectors",
        )
        workspaces.append(
            WorkspaceSpec(
                workspace_id=workspace_id,
                datasets=datasets,
                connectors=connectors,
            )
        )

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
        public_base_url=public_base_url,
        workspace_id=None,
        clerk_domain=clerk_domain,
        clerk_authorized_parties=clerk_authorized_parties,
        workspaces=tuple(workspaces),
        users=tuple(users),
        datasets=tuple(flat_datasets),
        search_database=search_database,
        providers=providers,
        search_warm=search_warm,
        max_concurrent_executions=max_concurrent_executions,
        extensions=extensions,
        allow_insecure_http=allow_insecure_http,
        include_dataset_docs_in_setup=include_dataset_docs_in_setup,
    )


def _parse_clerk_authorized_parties(auth: dict[str, Any]) -> tuple[str, ...]:
    if "clerk_authorized_parties" not in auth:
        raise ConfigError("auth.clerk_authorized_parties is required when auth.mode is clerk")
    raw = auth["clerk_authorized_parties"]
    if not isinstance(raw, list) or not raw:
        raise ConfigError("auth.clerk_authorized_parties must be a non-empty array of strings")
    parties: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        label = f"auth.clerk_authorized_parties[{index}]"
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{label} must be a non-empty string")
        party = item.strip()
        if party in seen:
            raise ConfigError(f"Duplicate auth.clerk_authorized_parties value: {party}")
        seen.add(party)
        parties.append(party)
    return tuple(parties)


def _parse_public_base_url(hosting: dict[str, Any], *, bind: str, port: int) -> str:
    from quail.config.hosting_url import default_public_base_url, normalize_public_base_url

    if "public_base_url" not in hosting:
        return default_public_base_url(bind=bind, port=port)
    value = hosting["public_base_url"]
    if not isinstance(value, str):
        raise ConfigError("hosting.public_base_url must be a string")
    try:
        return normalize_public_base_url(value)
    except ValueError as error:
        raise ConfigError(f"hosting.public_base_url: {error}") from error


def _parse_allow_public_unrestricted(hosting: dict[str, Any]) -> bool:
    if "allow_public_unrestricted" not in hosting:
        return False
    value = hosting["allow_public_unrestricted"]
    if not isinstance(value, bool):
        raise ConfigError("hosting.allow_public_unrestricted must be a boolean")
    return value


def _parse_allow_insecure_http(hosting: dict[str, Any]) -> bool:
    if "allow_insecure_http" not in hosting:
        return False
    value = hosting["allow_insecure_http"]
    if not isinstance(value, bool):
        raise ConfigError("hosting.allow_insecure_http must be a boolean")
    return value


def _parse_include_dataset_docs_in_setup(hosting: dict[str, Any]) -> bool:
    if "include_dataset_docs_in_setup" not in hosting:
        return False
    value = hosting["include_dataset_docs_in_setup"]
    if not isinstance(value, bool):
        raise ConfigError("hosting.include_dataset_docs_in_setup must be a boolean")
    return value


def _require_clerk_https_public_base_url(
    *,
    public_base_url: str,
    allow_insecure_http: bool,
) -> None:
    from quail.config.hosting_url import is_loopback_public_base_url

    if allow_insecure_http or is_loopback_public_base_url(public_base_url):
        return
    if urlparse(public_base_url).scheme != "https":
        raise ConfigError(
            "clerk mode requires hosting.public_base_url to be https for non-loopback "
            "origins; set hosting.allow_insecure_http = true to override"
        )


def _require_unrestricted_loopback(
    *,
    bind: str,
    public_base_url: str,
    allow_public_unrestricted: bool,
) -> None:
    from quail.config.hosting_url import is_loopback_host, is_loopback_public_base_url

    if allow_public_unrestricted:
        return
    if not is_loopback_host(bind):
        raise ConfigError(
            "unrestricted mode requires hosting.bind to be loopback "
            "(127.0.0.1, localhost, or ::1); set hosting.allow_public_unrestricted = true "
            "to expose an unauthenticated deployment deliberately"
        )
    if not is_loopback_public_base_url(public_base_url):
        raise ConfigError(
            "unrestricted mode requires hosting.public_base_url to be a loopback origin; "
            "set hosting.allow_public_unrestricted = true "
            "to expose an unauthenticated deployment deliberately"
        )


def _parse_max_concurrent_executions(hosting: dict[str, Any]) -> int:
    if "max_concurrent_executions" not in hosting:
        return _DEFAULT_MAX_CONCURRENT_EXECUTIONS
    value = hosting["max_concurrent_executions"]
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not (_MIN_MAX_CONCURRENT_EXECUTIONS <= value <= _MAX_MAX_CONCURRENT_EXECUTIONS)
    ):
        raise ConfigError(
            "hosting.max_concurrent_executions must be an integer from "
            f"{_MIN_MAX_CONCURRENT_EXECUTIONS} to {_MAX_MAX_CONCURRENT_EXECUTIONS}"
        )
    return value


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
        lexical_fields: tuple[str, ...] | None = None
        if "lexical" in entry:
            lexical_fields = _parse_lexical(entry["lexical"], label=f"{label}.lexical")
        datasets.append(
            DatasetSpec(
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                source=source,
                name=name,
                embedding=embedding,
                lexical_fields=lexical_fields,
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
    fields: tuple[str, ...] | None = None
    if "fields" in raw:
        fields = _parse_field_names(raw["fields"], label=f"{label}.fields")
    return EmbeddingProfile(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        dimensions=dimensions,
        revision=revision,
        fields=fields,
    )


def _parse_lexical(raw: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be a table")
    _reject_unknown(raw, _ALLOWED_LEXICAL, label=label)
    if "fields" not in raw:
        raise ConfigError(f"{label}.fields is required when {label} is set")
    return _parse_field_names(raw["fields"], label=f"{label}.fields")


def _parse_field_names(raw: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{label} must be a non-empty array of field names")
    names: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{label}[{index}] must be a non-empty string")
        name = item.strip()
        if name in seen:
            raise ConfigError(f"{label} duplicates {name!r}")
        seen.add(name)
        names.append(name)
    return tuple(names)


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
    has_lexical = False
    for spec in config.datasets:
        if spec.embedding is not None:
            needed_providers.add(spec.embedding.provider)
        if spec.lexical_fields is not None:
            has_lexical = True
    if config.search_database is None:
        if needed_providers:
            raise ConfigError(
                "core.search_database is required when any dataset declares [datasets.embedding]"
            )
        if has_lexical:
            raise ConfigError(
                "core.search_database is required when any dataset declares [datasets.lexical]"
            )
        return
    if not needed_providers:
        return
    if "ollama" in needed_providers and config.providers.ollama is None:
        raise ConfigError("[providers.ollama] is required for datasets using provider ollama")
    if "openrouter" in needed_providers and config.providers.openrouter is None:
        raise ConfigError(
            "[providers.openrouter] is required for datasets using provider openrouter"
        )


def _parse_extensions(raw: object) -> tuple[ExtensionPin, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("[[extensions]] must be an array of tables")
    pins: list[ExtensionPin] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        label = f"extensions[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be a table")
        _reject_unknown(entry, _ALLOWED_EXTENSION, label=label)
        extension_id = _require_string(entry, "id", label=f"{label}.id")
        if extension_id in seen:
            raise ConfigError(f"Duplicate extension id: {extension_id}")
        seen.add(extension_id)
        version = _require_string(entry, "version", label=f"{label}.version")
        pins.append(ExtensionPin(extension_id=extension_id, version=version))
    return tuple(pins)


def _parse_connectors(
    raw: object,
    *,
    extension_ids: set[str],
    dataset_ids: set[str],
    label_prefix: str,
) -> tuple[ConnectorBinding, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"[[{label_prefix}]] must be an array of tables")
    bindings: list[ConnectorBinding] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        label = f"{label_prefix}[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be a table")
        _reject_unknown(entry, _ALLOWED_CONNECTOR, label=label)
        extension_id = _require_string(entry, "id", label=f"{label}.id")
        if extension_id not in extension_ids:
            raise ConfigError(f"{label}.id is not pinned in [[extensions]]: {extension_id}")
        if extension_id in seen:
            raise ConfigError(f"Duplicate connector id in {label_prefix}: {extension_id}")
        seen.add(extension_id)
        config_raw = entry.get("config", {})
        if config_raw is None:
            config_raw = {}
        if not isinstance(config_raw, dict):
            raise ConfigError(f"{label}.config must be a table")
        config = _json_shaped_config(config_raw, label=f"{label}.config")
        bound_datasets = entry.get("datasets", [])
        if bound_datasets is None:
            bound_datasets = []
        if not isinstance(bound_datasets, list):
            raise ConfigError(f"{label}.datasets must be an array of tables")
        ids: list[str] = []
        seen_ds: set[str] = set()
        for d_index, ds_entry in enumerate(bound_datasets):
            ds_label = f"{label}.datasets[{d_index}]"
            if not isinstance(ds_entry, dict):
                raise ConfigError(f"{ds_label} must be a table")
            if set(ds_entry) - {"id"}:
                raise ConfigError(f"{ds_label} only allows id")
            dataset_id = _require_string(ds_entry, "id", label=f"{ds_label}.id")
            if dataset_id not in dataset_ids:
                raise ConfigError(f"{ds_label}.id is not a dataset in this workspace: {dataset_id}")
            if dataset_id in seen_ds:
                raise ConfigError(f"{ds_label} duplicates {dataset_id}")
            seen_ds.add(dataset_id)
            ids.append(dataset_id)
        bindings.append(
            ConnectorBinding(
                extension_id=extension_id,
                config=config,
                dataset_ids=tuple(ids),
            )
        )
    return tuple(bindings)


def _json_shaped_config(raw: dict[str, Any], *, label: str, depth: int = 0) -> dict[str, object]:
    if depth > 64:
        raise ConfigError(f"{label} exceeds max nesting depth")
    result: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise ConfigError(f"{label} keys must be non-empty strings")
        result[key] = _json_shaped_value(value, label=f"{label}.{key}", depth=depth + 1)
    return result


def _json_shaped_value(value: object, *, label: str, depth: int) -> object:
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ConfigError(f"{label} must be a finite number")
        return value
    if isinstance(value, list):
        return [
            _json_shaped_value(item, label=f"{label}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return _json_shaped_config(value, label=label, depth=depth)
    raise ConfigError(f"{label} has unsupported TOML type for connector config")


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
