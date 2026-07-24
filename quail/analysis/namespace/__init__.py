"""Public exports for ``quail.analysis.namespace``."""

from quail.analysis.bindings import RESERVED_NAMES

from .namespace import api_namespace

__all__ = [
    "RESERVED_NAMES",
    "api_namespace",
]
