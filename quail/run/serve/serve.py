"""Serve MCP after leasing deployment state (never activates)."""

from __future__ import annotations

from quail.analysis.admission import configure_execution_slots
from quail.auth.clerk import TokenVerifier
from quail.config.models import QuailConfig
from quail.datasets import open_core_db
from quail.mcp import create_mcp_server_from_config
from quail.run.apply import import_declared_datasets
from quail.run.lease import acquire_deployment_lease
from quail.run.process import assert_search_warm


def serve(config: QuailConfig, *, verifier: TokenVerifier | None = None) -> None:
    """Lease, import without activate, gate on warm + active match, then MCP."""

    configure_execution_slots(config.max_concurrent_executions)
    with acquire_deployment_lease(config):
        db = open_core_db(config.database)
        try:
            refs = import_declared_datasets(config, db, activate=False)
            assert_search_warm(db, config, refs)
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
