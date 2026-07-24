"""Embedding provider HTTP clients."""

from __future__ import annotations

from typing import Protocol, Sequence

from quail.config.models import EmbeddingProfile, ProvidersConfig
from quail.providers.errors import ProviderError
from quail.providers.ollama import OllamaEmbedder
from quail.providers.openrouter import OpenRouterEmbedder
from quail.providers.secrets import resolve_env_ref


class EmbeddingClient(Protocol):
    """Texts in, same-length list of float vectors out."""

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


def build_embedding_client(
    profile: EmbeddingProfile,
    providers: ProvidersConfig,
) -> EmbeddingClient:
    """Build an HTTP embedder for a dataset embedding profile."""

    if profile.provider == "ollama":
        if providers.ollama is None:
            raise ProviderError(
                "Ollama provider is not configured",
                repair_hint="Add [providers.ollama] with base_url, then restart Quail.",
            )
        return OllamaEmbedder(
            base_url=providers.ollama.base_url,
            model=profile.model,
            dimensions=profile.dimensions,
        )
    if profile.provider == "openrouter":
        if providers.openrouter is None:
            raise ProviderError(
                "OpenRouter provider is not configured",
                repair_hint="Add [providers.openrouter] with base_url and api_key, then restart Quail.",
            )
        api_key = resolve_env_ref(
            providers.openrouter.api_key,
            label="providers.openrouter.api_key",
        )
        return OpenRouterEmbedder(
            base_url=providers.openrouter.base_url,
            api_key=api_key,
            model=profile.model,
            dimensions=profile.dimensions,
        )
    raise ProviderError(f"Unsupported embedding provider: {profile.provider}")
