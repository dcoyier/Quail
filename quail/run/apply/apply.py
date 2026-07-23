"""Apply slim config: ensure workspace and import declared CSVs."""

from __future__ import annotations

from quail.config.errors import ConfigError
from quail.config.models import QuailConfig
from quail.datasets import ensure_workspace, import_csv_dataset, open_core_db
from quail.datasets.db import CoreDb


def apply_config(config: QuailConfig) -> CoreDb:
    """Open the core DB, ensure workspace, import each declared CSV (activate)."""

    for spec in config.datasets:
        if not spec.source.is_file():
            raise ConfigError(f"Dataset source not found: {spec.source}")

    db = open_core_db(config.database)
    try:
        ensure_workspace(db, config.workspace_id)
        for spec in config.datasets:
            import_csv_dataset(
                db,
                config.workspace_id,
                spec.dataset_id,
                spec.source,
                name=spec.name,
                activate=True,
            )
    except Exception:
        db.close()
        raise
    return db
