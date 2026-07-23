"""Serve unrestricted loopback MCP after applying slim config."""

from __future__ import annotations

from quail.config.models import QuailConfig
from quail.mcp import create_mcp_server
from quail.run.apply import apply_config


def serve(config: QuailConfig) -> None:
    """Apply declared state, then block on streamable-http MCP."""

    db = apply_config(config)
    # create_mcp_server opens its own DB handle; close the apply handle.
    db.close()
    server = create_mcp_server(
        config.database,
        config.feedback,
        workspace_id=config.workspace_id,
        host=config.bind,
        port=config.port,
    )
    server.run(transport="streamable-http")


def run_from_config(config: QuailConfig) -> None:
    """Alias for serve (CLI entry)."""

    serve(config)
