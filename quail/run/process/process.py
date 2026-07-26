"""Operator process: apply CSV import then warm search for declared datasets."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from quail.analysis.errors import QuailRuntimeError
from quail.config.errors import ConfigError
from quail.config.models import EmbeddingProfile, QuailConfig
from quail.datasets import active_version
from quail.datasets.db import CoreDb
from quail.providers import EmbeddingClient, build_embedding_client
from quail.run.apply import apply_config
from quail.search import open_search_db
from quail.search.warm import WarmDatasetResult, require_warm_ready, warm_dataset


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """Results from one quail process invocation."""

    results: tuple[WarmDatasetResult, ...]


def process_config(
    config: QuailConfig,
    *,
    clear: bool = False,
    embedder_factory: Callable[[EmbeddingProfile], EmbeddingClient] | None = None,
) -> ProcessOutcome:
    """Import/pin declared datasets, then warm Lexical and embeddings."""

    has_embedding = any(spec.embedding is not None for spec in config.datasets)
    if has_embedding and config.search_database is None:
        raise ConfigError("core.search_database is required when any dataset declares embedding")
    if clear and config.search_database is None:
        raise ConfigError("core.search_database is required for quail process --clear")

    db = apply_config(config)
    if config.search_database is None:
        db.close()
        return ProcessOutcome(results=())

    search = open_search_db(config.search_database)
    factory = embedder_factory or (
        lambda profile: build_embedding_client(profile, config.providers)
    )
    results: list[WarmDatasetResult] = []
    try:
        for spec in config.datasets:
            version = _require_active_version(db, spec.workspace_id, spec.dataset_id)
            result = warm_dataset(
                db,
                search,
                workspace_id=spec.workspace_id,
                dataset_id=spec.dataset_id,
                version_id=version,
                profile=spec.embedding,
                warm=config.search_warm,
                embedder_factory=factory,
                clear=clear,
                lexical_fields=spec.lexical_fields,
            )
            results.append(result)
    except Exception:
        search.close()
        db.close()
        raise
    search.close()
    db.close()
    return ProcessOutcome(results=tuple(results))


def assert_search_warm(db: CoreDb, config: QuailConfig) -> None:
    """Fail closed when search warm receipts do not match TOML."""

    if config.search_database is None:
        return
    search = open_search_db(config.search_database)
    try:
        for spec in config.datasets:
            version = _require_active_version(db, spec.workspace_id, spec.dataset_id)
            require_warm_ready(
                search,
                workspace_id=spec.workspace_id,
                dataset_id=spec.dataset_id,
                version_id=version,
                profile=spec.embedding,
                lexical_fields=spec.lexical_fields,
            )
    finally:
        search.close()


def _require_active_version(db: CoreDb, workspace_id: str, dataset_id: str) -> str:
    active = active_version(db, workspace_id, dataset_id)
    if active is None:
        raise QuailRuntimeError(
            f"Dataset {dataset_id!r} has no active version after apply",
            repair_hint="Confirm the CSV import succeeded, then re-run quail process.",
        )
    return active.version_id
