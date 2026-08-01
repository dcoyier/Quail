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
    # None = embed every non-empty source field.
    fields: tuple[str, ...] | None = None

    def profile_hash(self) -> str:
        payload = {
            "provider": self.provider,
            "model": self.model,
            "dimensions": self.dimensions,
            "revision": self.revision,
            "fields": list(self.fields) if self.fields is not None else None,
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
class ExtensionPin:
    """Deployment-wide trusted connector package identity."""

    extension_id: str
    version: str


@dataclass(frozen=True, slots=True)
class ConnectorBinding:
    """One connector activated in one workspace."""

    extension_id: str
    config: dict[str, object]
    dataset_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """One CSV dataset declared in the manifest."""

    workspace_id: str
    dataset_id: str
    source: Path
    name: str | None
    embedding: EmbeddingProfile | None = None
    # None = Lexical-index every non-empty source field.
    lexical_fields: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceSpec:
    """One workspace and its declared datasets."""

    workspace_id: str
    datasets: tuple[DatasetSpec, ...]
    connectors: tuple[ConnectorBinding, ...] = ()


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
    public_base_url: str
    # Unrestricted: fixed workspace. Clerk: None.
    workspace_id: str | None
    clerk_domain: str | None
    # Clerk: JWT azp/aud values that may present tokens (empty when unrestricted).
    clerk_authorized_parties: tuple[str, ...]
    workspaces: tuple[WorkspaceSpec, ...]
    users: tuple[UserSpec, ...]
    # Flattened datasets for apply (every mode).
    datasets: tuple[DatasetSpec, ...]
    search_database: Path | None = None
    providers: ProvidersConfig = ProvidersConfig()
    search_warm: SearchWarmConfig = SearchWarmConfig()
    max_concurrent_executions: int = 2
    extensions: tuple[ExtensionPin, ...] = ()
    # Unrestricted only: allow non-loopback bind / public_base_url (dangerous).
    allow_public_unrestricted: bool = False
    # Clerk only: allow non-loopback http:// public_base_url (dangerous).
    allow_insecure_http: bool = False
    # When true, quail_setup embeds connector dataset docs in each catalog row.
    include_dataset_docs_in_setup: bool = False
