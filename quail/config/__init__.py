"""Slim hand-edited quail.toml loading."""

from quail.config.errors import ConfigError
from quail.config.models import (
    AuthMode,
    DatasetSpec,
    EmbeddingProfile,
    EmbeddingProviderName,
    OllamaProvider,
    OpenRouterProvider,
    ProvidersConfig,
    QuailConfig,
    SearchWarmConfig,
    UserSpec,
    WorkspaceSpec,
)
from quail.config.parse import load_config, parse_config

__all__ = [
    "AuthMode",
    "ConfigError",
    "DatasetSpec",
    "EmbeddingProfile",
    "EmbeddingProviderName",
    "OllamaProvider",
    "OpenRouterProvider",
    "ProvidersConfig",
    "QuailConfig",
    "SearchWarmConfig",
    "UserSpec",
    "WorkspaceSpec",
    "load_config",
    "parse_config",
]
