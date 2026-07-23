"""Thin FastMCP loopback adapter over host analysis APIs."""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from quail.analysis.exec_host import exec_script
from quail.datasets import get_dataset, list_datasets, open_core_db
from quail.mcp.context import DEFAULT_WORKSPACE_ID, McpContext
from quail.mcp.feedback import append_feedback
from quail.mcp.results import (
    error_result,
    success_printed_output,
    success_result,
    validate_time_window,
)
from quail.session import create_session, get_session
from quail.session.sessions import require_active_session

# Repo root: quail/mcp/server/server.py → parents[3]
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_API_DOCS = _REPO_ROOT / "docs" / "api.md"

_DATASET_INFO_STUB = (
    "Use quail_exec with this dataset_id. Imported source data is immutable; "
    "analysis tags and bindings stay on the session."
)


def create_mcp_server(
    db_path: str | Path,
    feedback_path: str | Path,
    *,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    api_docs_path: str | Path | None = None,
) -> FastMCP:
    """Build an unrestricted loopback FastMCP app with the six core tools."""

    docs_path = Path(api_docs_path) if api_docs_path is not None else _DEFAULT_API_DOCS
    db = open_core_db(db_path)
    context = McpContext(
        db=db,
        workspace_id=workspace_id,
        feedback_path=Path(feedback_path),
        api_docs_path=docs_path,
    )
    server = FastMCP(
        "quail",
        instructions=(
            "Quail analysis tools for a fixed unrestricted workspace. "
            "Read quail_get_api_docs before writing quail_exec code. "
            "Use provide_feedback for friction or improvement notes."
        ),
        host="127.0.0.1",
    )
    _register_tools(server, context)
    return server


def _register_tools(server: FastMCP, context: McpContext) -> None:
    @server.tool(title="Get Quail API docs")
    def quail_get_api_docs() -> CallToolResult:
        """Return the analysis language documentation for quail_exec."""

        try:
            documentation = context.api_docs_path.read_text(encoding="utf-8")
        except Exception as error:
            return error_result(error=error, repair_hint="Ensure docs/api.md is readable.")
        return success_result({"documentation": documentation})

    @server.tool(title="List Quail datasets")
    def quail_list_datasets() -> CallToolResult:
        """List datasets available in the fixed workspace."""

        try:
            datasets = [
                {
                    "dataset_id": ref.dataset_id,
                    "name": ref.name,
                    "active_version_id": ref.active_version_id,
                }
                for ref in list_datasets(context.db, context.workspace_id)
            ]
        except Exception as error:
            return error_result(error=error)
        return success_result({"datasets": datasets})

    @server.tool(title="Start Quail session")
    def quail_start_session() -> CallToolResult:
        """Create an active analysis session in the fixed workspace."""

        try:
            session = create_session(context.db, context.workspace_id)
        except Exception as error:
            return error_result(error=error)
        return success_result(
            {
                "session_id": session.id,
                "workspace_id": session.workspace_id,
                "state_revision": session.state_revision,
            }
        )

    @server.tool(title="Get Quail dataset info")
    def quail_get_dataset_info(dataset_id: str) -> CallToolResult:
        """Return dataset name/id plus short documentation guidance."""

        try:
            ref = get_dataset(context.db, context.workspace_id, dataset_id)
            if ref is None:
                raise ValueError(f"Dataset not found: {dataset_id}")
            display = ref.name or ref.dataset_id
            documentation = f"Dataset {display} ({ref.dataset_id}). {_DATASET_INFO_STUB}"
        except Exception as error:
            return error_result(error=error)
        return success_result(
            {
                "dataset_id": ref.dataset_id,
                "name": ref.name,
                "active_version_id": ref.active_version_id,
                "documentation": documentation,
            }
        )

    @server.tool(title="Execute Quail analysis")
    def quail_exec(
        session_id: str,
        dataset_id: str,
        code: str,
        time_window: str | None = "standard",
    ) -> CallToolResult:
        """Run bounded Quail Python for one session and dataset."""

        try:
            validate_time_window(time_window)
            session = require_active_session(context.db, session_id)
            if session.workspace_id != context.workspace_id:
                raise ValueError("Session does not belong to this workspace")
            outcome = exec_script(
                context.db,
                session_id=session_id,
                dataset_id=dataset_id,
                expected_revision=session.state_revision,
                code=code,
            )
        except Exception as error:
            return error_result(
                error=error,
                execution_id=None,
                repair_hint=(
                    "Fix the diagnostic, keep the same session_id, and retry. "
                    "Failed exec does not commit tags or bindings."
                ),
            )
        return success_printed_output(outcome.printed_output)

    @server.tool(title="Provide feedback")
    def provide_feedback(
        message: str,
        category: str | None = None,
        session_id: str | None = None,
        dataset_id: str | None = None,
    ) -> CallToolResult:
        """Append friction or improvement notes outside the core analysis DB."""

        try:
            if session_id is not None:
                session = get_session(context.db, session_id)
                if session is None:
                    raise ValueError(f"Session not found: {session_id}")
            if dataset_id is not None:
                ref = get_dataset(context.db, context.workspace_id, dataset_id)
                if ref is None:
                    raise ValueError(f"Dataset not found: {dataset_id}")
            append_feedback(
                context.feedback_path,
                workspace_id=context.workspace_id,
                message=message,
                category=category,
                session_id=session_id,
                dataset_id=dataset_id,
            )
        except Exception as error:
            return error_result(error=error)
        return success_result({"accepted": True})
