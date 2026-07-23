"""Operator run: apply slim TOML then serve MCP."""

from quail.run.apply import apply_config
from quail.run.serve import run_from_config, serve

__all__ = [
    "apply_config",
    "run_from_config",
    "serve",
]
