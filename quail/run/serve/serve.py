"""Serve MCP after applying slim config (unrestricted or Clerk)."""

from __future__ import annotations

from quail.auth.clerk import TokenVerifier
from quail.config.models import QuailConfig
from quail.mcp import create_mcp_server_from_config
from quail.run.apply import apply_config
from quail.run.process import assert_search_warm


def serve(config: QuailConfig, *, verifier: TokenVerifier | None = None) -> None:
    """Apply declared state, gate on search warm, then block on MCP."""

    db = apply_config(config)
    try:
        assert_search_warm(db, config)
    finally:
        db.close()
    server = create_mcp_server_from_config(config, verifier=verifier)
    server.run(transport="streamable-http")


def run_from_config(
    config: QuailConfig,
    *,
    verifier: TokenVerifier | None = None,
) -> None:
    """Alias for serve (CLI entry)."""

    serve(config, verifier=verifier)
