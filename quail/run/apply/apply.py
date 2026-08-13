"""Apply slim config: ensure workspaces and import declared CSVs."""

from __future__ import annotations

from quail.config.errors import ConfigError
from quail.config.models import QuailConfig
from quail.datasets import DatasetRef, ensure_workspace, import_csv_dataset, open_core_db
from quail.datasets.db import CoreDb
from quail.run.lease import acquire_deployment_lease


def apply_config(config: QuailConfig) -> CoreDb:
    """Open the core DB, ensure workspaces, import each declared CSV (activate).

    Embedding pins are applied by ``process_config`` publication, not here.
    Takes the deployment lease for the import so it cannot race process/serve.
    """

    for spec in config.datasets:
        if not spec.source.is_file():
            raise ConfigError(f"Dataset source not found: {spec.source}")

    with acquire_deployment_lease(config):
        db = open_core_db(config.database)
        try:
            import_declared_datasets(config, db, activate=True)
        except Exception:
            db.close()
            raise
        return db


def import_declared_datasets(
    config: QuailConfig,
    db: CoreDb,
    *,
    activate: bool = True,
) -> tuple[DatasetRef, ...]:
    """Ensure workspaces and import each declared CSV.

    When ``activate`` is False, versions are imported ready but left inactive
    so callers can warm before a publication pass.
    """

    for spec in config.datasets:
        if not spec.source.is_file():
            raise ConfigError(f"Dataset source not found: {spec.source}")

    for workspace in config.workspaces:
        ensure_workspace(db, workspace.workspace_id)

    refs: list[DatasetRef] = []
    for spec in config.datasets:
        ref = import_csv_dataset(
            db,
            spec.workspace_id,
            spec.dataset_id,
            spec.source,
            name=spec.name,
            activate=activate,
        )
        refs.append(ref)
    return tuple(refs)
