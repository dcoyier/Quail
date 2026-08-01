"""Operator process: import, warm search, then publish activation and pins."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from quail.analysis.errors import QuailRuntimeError
from quail.config.errors import ConfigError
from quail.config.models import EmbeddingProfile, QuailConfig
from quail.datasets import (
    DatasetRef,
    activate_dataset_version,
    active_version,
    open_core_db,
)
from quail.datasets.db import CoreDb
from quail.providers import EmbeddingClient, build_embedding_client
from quail.run.apply import import_declared_datasets
from quail.run.lease import acquire_deployment_lease
from quail.search import open_search_db
from quail.search.db import SearchDb
from quail.search.lexical.corpus import sweep_scratch_corpora
from quail.search.pin import delete_embedding_pin, pin_embedding_profile
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
    """Lease, import without activate, warm all, then publish activation and pins."""

    has_embedding = any(spec.embedding is not None for spec in config.datasets)
    if has_embedding and config.search_database is None:
        raise ConfigError("core.search_database is required when any dataset declares embedding")
    if clear and config.search_database is None:
        raise ConfigError("core.search_database is required for quail process --clear")

    with acquire_deployment_lease(config):
        db = open_core_db(config.database)
        try:
            refs = import_declared_datasets(config, db, activate=False)
            if config.search_database is None:
                _publish_activation_and_pins(db, config, refs, search=None)
                return ProcessOutcome(results=())

            search = open_search_db(config.search_database)
            factory = embedder_factory or (
                lambda profile: build_embedding_client(profile, config.providers)
            )
            results: list[WarmDatasetResult] = []
            try:
                sweep_scratch_corpora(search)
                for spec, ref in zip(config.datasets, refs, strict=True):
                    result = warm_dataset(
                        db,
                        search,
                        workspace_id=spec.workspace_id,
                        dataset_id=spec.dataset_id,
                        version_id=ref.version_id,
                        profile=spec.embedding,
                        warm=config.search_warm,
                        embedder_factory=factory,
                        clear=clear,
                        lexical_fields=spec.lexical_fields,
                    )
                    results.append(result)
                _publish_activation_and_pins(db, config, refs, search=search)
            except Exception:
                search.close()
                raise
            search.close()
            return ProcessOutcome(results=tuple(results))
        finally:
            db.close()


def assert_search_warm(
    db: CoreDb,
    config: QuailConfig,
    refs: Sequence[DatasetRef] | None = None,
) -> None:
    """Fail closed when search warm receipts do not match TOML.

    When ``refs`` is provided (serve path), also require each imported version
    to already be the active version — serve never activates.
    """

    if refs is not None:
        if len(refs) != len(config.datasets):
            raise QuailRuntimeError(
                "Imported dataset count does not match quail.toml",
                repair_hint="Re-run quail process with the current quail.toml.",
            )
        for spec, ref in zip(config.datasets, refs, strict=True):
            active = active_version(db, spec.workspace_id, spec.dataset_id)
            if active is None or active.version_id != ref.version_id:
                raise QuailRuntimeError(
                    f"Dataset {spec.dataset_id!r} imported version is not active",
                    repair_hint="Run quail process with this quail.toml, then restart quail run.",
                )

    if config.search_database is None:
        return
    search = open_search_db(config.search_database)
    try:
        sweep_scratch_corpora(search)
        for index, spec in enumerate(config.datasets):
            if refs is not None:
                version = refs[index].version_id
            else:
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


def _publish_activation_and_pins(
    db: CoreDb,
    config: QuailConfig,
    refs: Sequence[DatasetRef],
    *,
    search: SearchDb | None,
) -> None:
    """Activate every imported version, then add or delete embedding pins."""

    for spec, ref in zip(config.datasets, refs, strict=True):
        activate_dataset_version(db, spec.workspace_id, spec.dataset_id, ref.version_id)

    if search is None:
        return
    for spec, ref in zip(config.datasets, refs, strict=True):
        if spec.embedding is not None:
            pin_embedding_profile(
                search,
                workspace_id=spec.workspace_id,
                dataset_id=spec.dataset_id,
                version_id=ref.version_id,
                profile=spec.embedding,
            )
        else:
            delete_embedding_pin(
                search,
                workspace_id=spec.workspace_id,
                dataset_id=spec.dataset_id,
                version_id=ref.version_id,
            )


def _require_active_version(db: CoreDb, workspace_id: str, dataset_id: str) -> str:
    active = active_version(db, workspace_id, dataset_id)
    if active is None:
        raise QuailRuntimeError(
            f"Dataset {dataset_id!r} has no active version after apply",
            repair_hint="Confirm the CSV import succeeded, then re-run quail process.",
        )
    return active.version_id
