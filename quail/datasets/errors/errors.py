"""Dataset catalog and CSV import errors."""

from __future__ import annotations


class DatasetError(Exception):
    """Base failure for dataset catalog and CSV import."""


class DatasetSyntaxError(DatasetError):
    """Invalid CSV shape, ids, or catalog arguments."""


class DatasetConflictError(DatasetError):
    """Immutable version identity conflicts with stored data."""
