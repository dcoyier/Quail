"""Slim hand-edited quail.toml loading."""

from quail.config.errors import ConfigError
from quail.config.models import DatasetSpec, QuailConfig
from quail.config.parse import load_config, parse_config

__all__ = [
    "ConfigError",
    "DatasetSpec",
    "QuailConfig",
    "load_config",
    "parse_config",
]
