"""Slim quail.toml models."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

AuthMode = Literal["unrestricted", "clerk"]
EmbeddingProviderName = Literal["ollama", "openrouter"]


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    """Per-dataset embedding identity used for Semantic cache keys."""

    provider: EmbeddingProviderName
    model: str
    dimensions: int
    revision: str

    def profile_hash(self) -> str:
        payload = {
            "provider": self.provider,
            "model": self.model,
            "dimensions": self.dimensions,
            "revision": self.revision,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class OllamaProvider:
    """Deployment-wide Ollama connectivity."""

    base_url: str


@dataclass(frozen=True, slots=True)
class OpenRouterProvider:
    """Deployment-wide OpenRouter connectivity (api_key is an env: ref)."""

    base_url: str
    api_key: str


@dataclass(frozen=True, slots=True)
class ProvidersConfig:
    """Optional embedding provider connectivity blocks."""

    ollama: OllamaProvider | None = None
    openrouter: OpenRouterProvider | None = None


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """One CSV dataset declared in the manifest."""

    workspace_id: str
    dataset_id: str
    source: Path
    name: str | None
    embedding: EmbeddingProfile | None = None


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
class SearchWarmConfig:
    """Batch and concurrency knobs for quail process embedding warm."""

    embed_batch_size: int = 32
    max_concurrent_embed_requests: int = 2


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
    search_database: Path | None = None
    providers: ProvidersConfig = ProvidersConfig()
    search_warm: SearchWarmConfig = SearchWarmConfig()
    max_concurrent_executions: int = 2
