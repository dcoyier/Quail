"""Immutable dataset catalog models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SourceField:
    name: str
    position: int


@dataclass(frozen=True, slots=True)
class SourceEntry:
    id: str
    position: int


@dataclass(frozen=True, slots=True)
class ActiveVersion:
    version_id: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class DatasetRef:
    workspace_id: str
    dataset_id: str
    name: str | None
    active_version_id: str | None
    version_id: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class CsvDataset:
    path: Path
    version_id: str
    content_hash: str
    byte_count: int
    cell_count: int
    estimated_durable_bytes: int
    field_names: tuple[str, ...]
    entries: tuple[dict[str, Any], ...]
