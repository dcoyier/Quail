"""Public exports for ``quail.providers``."""

from quail.providers.errors import ProviderError
from quail.providers.factory import EmbeddingClient, build_embedding_client
from quail.providers.ollama import OllamaEmbedder
from quail.providers.openrouter import OpenRouterEmbedder
from quail.providers.secrets import resolve_env_ref

__all__ = [
    "EmbeddingClient",
    "OllamaEmbedder",
    "OpenRouterEmbedder",
    "ProviderError",
    "build_embedding_client",
    "resolve_env_ref",
]
