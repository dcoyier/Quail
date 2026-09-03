"""Quail core. Code follows docs/; this package is built one module at a time."""

from quail.project import (
    DatasetSpec,
    KernelLimits,
    Manifest,
    Project,
    ProviderSpec,
    QuailError,
)

__all__ = [
    "DatasetSpec",
    "KernelLimits",
    "Manifest",
    "Project",
    "ProviderSpec",
    "QuailError",
]
