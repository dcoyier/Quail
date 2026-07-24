"""Apply slim config: ensure workspaces and import declared CSVs."""

from __future__ import annotations

from quail.config.errors import ConfigError
from quail.config.models import QuailConfig
from quail.datasets import ensure_workspace, import_csv_dataset, open_core_db
from quail.datasets.db import CoreDb


def apply_config(config: QuailConfig) -> CoreDb:
    """Open the core DB, ensure workspaces, import each declared CSV (activate)."""

    for spec in config.datasets:
        if not spec.source.is_file():
            raise ConfigError(f"Dataset source not found: {spec.source}")

    db = open_core_db(config.database)
    try:
        for workspace in config.workspaces:
            ensure_workspace(db, workspace.workspace_id)
        for spec in config.datasets:
            import_csv_dataset(
                db,
                spec.workspace_id,
                spec.dataset_id,
                spec.source,
                name=spec.name,
                activate=True,
            )
    except Exception:
        db.close()
        raise
    return db
