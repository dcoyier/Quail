"""Session, scope, and staged overlay mutation models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    workspace_id: str
    status: str
    state_revision: int


@dataclass(frozen=True, slots=True)
class Scope:
    session_id: str
    workspace_id: str
    dataset_id: str
    dataset_version_id: str


@dataclass(frozen=True, slots=True)
class AnalysisField:
    name: str
    position: int
    value_revision: int


@dataclass(frozen=True, slots=True)
class CatalogField:
    name: str
    kind: str
    position: int


@dataclass(frozen=True, slots=True)
class FieldCreate:
    name: str


@dataclass(frozen=True, slots=True)
class ValueWrite:
    field: str
    entry_id: str
    value: Any


@dataclass(frozen=True, slots=True)
class ValueDelete:
    field: str
    entry_id: str
    value: Any | None = None
