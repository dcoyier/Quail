"""Public exports for ``quail.datasets.db``."""

from .db import CoreDb, ensure_workspace, open_core_db

__all__ = [
    "CoreDb",
    "ensure_workspace",
    "open_core_db",
]
