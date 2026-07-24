"""Slim quail.toml models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

AuthMode = Literal["unrestricted", "clerk"]


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """One CSV dataset declared in the manifest."""

    workspace_id: str
    dataset_id: str
    source: Path
    name: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceSpec:
    """One workspace and its declared datasets."""

    workspace_id: str
    datasets: tuple[DatasetSpec, ...]


@dataclass(frozen=True, slots=True)
class UserSpec:
    """TOML allowlisted Clerk user."""

    user_id: str
    clerk_user_id: str
    workspaces: tuple[str, ...]
    default_workspace: str | None
    lock_workspace: bool


@dataclass(frozen=True, slots=True)
class QuailConfig:
    """Resolved slim deployment config for quail run."""

    manifest_path: Path
    database: Path
    feedback: Path
    auth_mode: AuthMode
    bind: str
    port: int
    # Unrestricted: fixed workspace. Clerk: None.
    workspace_id: str | None
    clerk_domain: str | None
    workspaces: tuple[WorkspaceSpec, ...]
    users: tuple[UserSpec, ...]
    # Flattened datasets for apply (every mode).
    datasets: tuple[DatasetSpec, ...]
