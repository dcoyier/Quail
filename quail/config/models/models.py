"""Slim quail.toml models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """One CSV dataset declared in the manifest."""

    dataset_id: str
    source: Path
    name: str | None


@dataclass(frozen=True, slots=True)
class QuailConfig:
    """Resolved slim deployment config for unrestricted loopback run."""

    manifest_path: Path
    database: Path
    feedback: Path
    workspace_id: str
    bind: str
    port: int
    datasets: tuple[DatasetSpec, ...]
