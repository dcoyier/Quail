"""Apply slim config: ensure workspaces and import declared CSVs."""

from __future__ import annotations

from quail.config.errors import ConfigError
from quail.config.models import QuailConfig
from quail.datasets import ensure_workspace, import_csv_dataset, open_core_db
from quail.datasets.db import CoreDb
from quail.search import open_search_db, pin_embedding_profile


def apply_config(config: QuailConfig) -> CoreDb:
    """Open the core DB, ensure workspaces, import each declared CSV (activate)."""

    for spec in config.datasets:
        if not spec.source.is_file():
            raise ConfigError(f"Dataset source not found: {spec.source}")

    db = open_core_db(config.database)
    search = None
    try:
        if config.search_database is not None:
            search = open_search_db(config.search_database)
        for workspace in config.workspaces:
            ensure_workspace(db, workspace.workspace_id)
        for spec in config.datasets:
            ref = import_csv_dataset(
                db,
                spec.workspace_id,
                spec.dataset_id,
                spec.source,
                name=spec.name,
                activate=True,
            )
            if spec.embedding is not None:
                if search is None:
                    raise ConfigError(
                        "core.search_database is required when any dataset declares embedding"
                    )
                pin_embedding_profile(
                    search,
                    workspace_id=spec.workspace_id,
                    dataset_id=spec.dataset_id,
                    version_id=ref.version_id,
                    profile=spec.embedding,
                )
    except Exception:
        db.close()
        raise
    finally:
        if search is not None:
            search.close()
    return db
