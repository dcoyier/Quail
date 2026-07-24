"""Thin FastMCP adapter over host analysis APIs (unrestricted + Clerk)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult

from quail.analysis.errors import QuailRuntimeError
from quail.analysis.exec_host import exec_script
from quail.auth import (
    AllowlistedPrincipal,
    AuthError,
    ClerkJwtVerifier,
    ForbiddenError,
    UnauthorizedError,
    authenticate_bearer,
)
from quail.auth.clerk import TokenVerifier
from quail.config.models import QuailConfig, UserSpec
from quail.datasets import get_dataset, list_datasets, open_core_db
from quail.mcp.bearer import get_bearer_override
from quail.mcp.context import DEFAULT_WORKSPACE_ID, McpContext
from quail.mcp.feedback import append_feedback
from quail.mcp.instructions import (
    LOCK_REPAIR_HINT,
    UNBOUND_REPAIR_HINT,
    clerk_instructions,
    unrestricted_instructions,
)
from quail.mcp.oauth import (
    ClerkAccessTokenVerifier,
    build_clerk_auth_settings,
    register_clerk_oauth_discovery,
)
from quail.mcp.offload import run_blocking
from quail.mcp.results import (
    error_result,
    success_printed_output,
    success_result,
    validate_time_window,
)
from quail.mcp.sticky import StickyWorkspaceStore
from quail.search.runtime import SearchRuntime, search_runtime_from_config
from quail.session import create_session, get_session
from quail.session.sessions import require_active_session

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_API_DOCS = _REPO_ROOT / "docs" / "api.md"

_DATASET_INFO_FALLBACK = (
    "No connector documentation is installed for this dataset. "
    "Use quail_exec inspection and quail_get_api_docs for the analysis language. "
    "Imported source data is immutable; analysis tags and bindings stay on the session."
)

_DEFAULT_EXEC_REPAIR = (
    "Fix the diagnostic, keep the same session_id, and retry. "
    "Failed exec does not commit tags or bindings."
)


@dataclass(slots=True)
class ClerkMcpRuntime:
    """Clerk-mode MCP dependencies shared by tools."""

    db_path: Path
    feedback_path: Path
    api_docs_path: Path
    users: tuple[UserSpec, ...]
    verifier: TokenVerifier
    sticky: StickyWorkspaceStore
    host: str
    port: int
    search_runtime: SearchRuntime | None = None


def create_mcp_server(
    db_path: str | Path,
    feedback_path: str | Path,
    *,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    api_docs_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    search_runtime: SearchRuntime | None = None,
    connector_catalog: Any | None = None,
) -> FastMCP:
    """Build an unrestricted loopback FastMCP app with the six core tools."""

    docs_path = Path(api_docs_path) if api_docs_path is not None else _DEFAULT_API_DOCS
    context = McpContext(
        db_path=Path(db_path).expanduser().resolve(),
        workspace_id=workspace_id,
        feedback_path=Path(feedback_path),
        api_docs_path=docs_path,
        search_runtime=search_runtime,
    )
    server = FastMCP(
        "quail",
        instructions=unrestricted_instructions(workspace_id),
        host=host,
        port=port,
    )
    _register_unrestricted_tools(server, context, connector_catalog=connector_catalog)
    if connector_catalog is not None:
        from quail.connectors.mcp_wire import register_connectors

        register_connectors(
            server,
            connector_catalog,
            resolve_workspace=lambda _ctx: (workspace_id, None),
        )
    return server


def create_mcp_server_from_config(
    config: QuailConfig,
    *,
    api_docs_path: str | Path | None = None,
    verifier: TokenVerifier | None = None,
) -> FastMCP:
    """Build MCP from slim config (unrestricted or Clerk)."""

    from quail.analysis.admission import configure_execution_slots

    configure_execution_slots(config.max_concurrent_executions)
    runtime = search_runtime_from_config(config)
    from quail.connectors.load import load_connector_catalog

    with open_core_db(config.database) as db:
        connector_catalog = load_connector_catalog(config, db)
    if config.auth_mode == "unrestricted":
        assert config.workspace_id is not None
        return create_mcp_server(
            config.database,
            config.feedback,
            workspace_id=config.workspace_id,
            api_docs_path=api_docs_path,
            host=config.bind,
            port=config.port,
            search_runtime=runtime,
            connector_catalog=connector_catalog,
        )
    assert config.clerk_domain is not None
    return create_clerk_mcp_server(
        config,
        api_docs_path=api_docs_path,
        verifier=verifier or ClerkJwtVerifier(config.clerk_domain),
        search_runtime=runtime,
        connector_catalog=connector_catalog,
    )


def create_clerk_mcp_server(
    config: QuailConfig,
    *,
    verifier: TokenVerifier,
    api_docs_path: str | Path | None = None,
    search_runtime: SearchRuntime | None = None,
    connector_catalog: Any | None = None,
) -> FastMCP:
    """Build Clerk-authenticated MCP with list/switch workspace tools."""

    docs_path = Path(api_docs_path) if api_docs_path is not None else _DEFAULT_API_DOCS
    if search_runtime is None:
        search_runtime = search_runtime_from_config(config)
    runtime = ClerkMcpRuntime(
        db_path=config.database,
        feedback_path=config.feedback,
        api_docs_path=docs_path,
        users=config.users,
        verifier=verifier,
        sticky=StickyWorkspaceStore(),
        host=config.bind,
        port=config.port,
        search_runtime=search_runtime,
    )
    assert config.clerk_domain is not None
    server = FastMCP(
        "quail",
        instructions=clerk_instructions(locked=False),
        host=config.bind,
        port=config.port,
        auth=build_clerk_auth_settings(
            clerk_domain=config.clerk_domain,
            public_base_url=config.public_base_url,
        ),
        token_verifier=ClerkAccessTokenVerifier(verifier),
    )
    register_clerk_oauth_discovery(server, clerk_domain=config.clerk_domain)
    _register_clerk_tools(server, runtime, connector_catalog=connector_catalog)
    if connector_catalog is not None:
        from quail.connectors.mcp_wire import register_connectors

        register_connectors(
            server,
            connector_catalog,
            resolve_workspace=lambda ctx: _resolve_clerk_connector_workspace(runtime, ctx),
        )
    return server


def _resolve_clerk_connector_workspace(
    runtime: ClerkMcpRuntime,
    ctx: Context | None,
) -> tuple[str, str | None]:
    """Resolve sticky workspace for a connector tool call."""

    authorization = _authorization_header(ctx)
    principal = authenticate_bearer(
        authorization,
        users=runtime.users,
        verifier=runtime.verifier,
    )
    session_id = _mcp_session_id(ctx)
    connection_key = (
        f"sess:{session_id}" if session_id is not None else f"user:{principal.clerk_user_id}"
    )
    workspace_id = runtime.sticky.active(connection_key)
    if workspace_id is None:
        workspace_id = runtime.sticky.ensure_initial_bind(connection_key, principal.user)
    if workspace_id is None:
        raise QuailRuntimeError(
            "No sticky workspace is bound for this connection.",
            repair_hint=UNBOUND_REPAIR_HINT,
        )
    if workspace_id not in principal.user.workspaces:
        raise ForbiddenError("Not a member of the active workspace")
    return workspace_id, principal.user.user_id


def _register_unrestricted_tools(
    server: FastMCP,
    context: McpContext,
    *,
    connector_catalog: Any | None = None,
) -> None:
    @server.tool(title="Get Quail API docs")
    async def quail_get_api_docs() -> CallToolResult:
        """Return the analysis-language docs for code inside quail_exec.

        Necessary for writing quail_exec code.
        Dataset and session workflow live in the other tools.
        """

        def work() -> CallToolResult:
            try:
                documentation = context.api_docs_path.read_text(encoding="utf-8")
            except Exception as error:
                return error_result(error=error, repair_hint="Ensure docs/api.md is readable.")
            return success_result({"documentation": documentation})

        return await run_blocking(work)

    @server.tool(title="List Quail datasets")
    async def quail_list_datasets() -> CallToolResult:
        """List datasets in this server's fixed workspace.

        Returns dataset_id, optional name, and active_version_id. Use a
        dataset_id with quail_get_dataset_info and quail_exec.
        """

        def work() -> CallToolResult:
            try:
                with open_core_db(context.db_path) as db:
                    datasets = [
                        {
                            "dataset_id": ref.dataset_id,
                            "name": ref.name,
                            "active_version_id": ref.active_version_id,
                        }
                        for ref in list_datasets(db, context.workspace_id)
                    ]
            except Exception as error:
                return error_result(error=error)
            return success_result({"datasets": datasets})

        return await run_blocking(work)

    @server.tool(title="Start Quail session")
    async def quail_start_session() -> CallToolResult:
        """Create an active analysis session in this workspace.

        The session belongs to this workspace (workspace_id is returned).
        Reuse session_id on quail_exec calls serially. Bindings and tags
        persist on this session across successful execs.
        """

        def work() -> CallToolResult:
            try:
                with open_core_db(context.db_path) as db:
                    session = create_session(db, context.workspace_id)
            except Exception as error:
                return error_result(error=error)
            return success_result(
                {
                    "session_id": session.id,
                    "workspace_id": session.workspace_id,
                    "state_revision": session.state_revision,
                }
            )

        return await run_blocking(work)

    @server.tool(title="Get Quail dataset info")
    async def quail_get_dataset_info(dataset_id: str) -> CallToolResult:
        """Return dataset identity plus short corpus/guidance documentation.

        Call this for dataset-specific notes before analyzing. Do not invent
        field meanings beyond what this tool and quail_exec inspection return.
        """

        def work() -> CallToolResult:
            try:
                with open_core_db(context.db_path) as db:
                    ref = get_dataset(db, context.workspace_id, dataset_id)
                    if ref is None:
                        raise ValueError(f"Dataset not found: {dataset_id}")
                    documentation = _dataset_documentation(
                        connector_catalog,
                        workspace_id=context.workspace_id,
                        user_id=None,
                        dataset_id=ref.dataset_id,
                        display_name=ref.name or ref.dataset_id,
                    )
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

        return await run_blocking(work)

    @server.tool(title="Execute Quail analysis")
    async def quail_exec(
        session_id: str,
        dataset_id: str,
        code: str,
        time_window: str | None = "standard",
    ) -> CallToolResult:
        """Run bounded Quail Python for one session and one dataset.

        code must follow quail_get_api_docs. Success: printed_output only.
        Failure: diagnostic with execution_id null; no tags/bindings/prints
        are kept. Reuse one session_id serially; do not overlap quail_exec
        calls on the same session_id. time_window is "standard" (30s wall /
        15s CPU) or "extended" (100s wall / 60s CPU); worker RSS is capped
        at 256 MiB.
        """

        def work() -> CallToolResult:
            try:
                validate_time_window(time_window)
                with open_core_db(context.db_path) as db:
                    session = require_active_session(db, session_id)
                    if session.workspace_id != context.workspace_id:
                        raise ValueError("Session does not belong to this workspace")
                    outcome = exec_script(
                        db,
                        session_id=session_id,
                        dataset_id=dataset_id,
                        expected_revision=session.state_revision,
                        code=code,
                        search_runtime=context.search_runtime,
                        time_window=time_window,
                    )
            except Exception as error:
                return error_result(
                    error=error,
                    execution_id=None,
                    repair_hint=_exec_repair_hint(error),
                )
            return success_printed_output(outcome.printed_output)

        return await run_blocking(work)

    @server.tool(title="Provide feedback")
    async def provide_feedback(
        message: str,
        category: str | None = None,
        session_id: str | None = None,
        dataset_id: str | None = None,
    ) -> CallToolResult:
        """Record friction or improvement notes outside the core analysis DB.

        Use when Quail was confusing, blocked you, or should improve — including
        expected outcomes that did not occur. Low bar for entry.
        """

        def work() -> CallToolResult:
            try:
                with open_core_db(context.db_path) as db:
                    if session_id is not None:
                        session = get_session(db, session_id)
                        if session is None:
                            raise ValueError(f"Session not found: {session_id}")
                    if dataset_id is not None:
                        ref = get_dataset(db, context.workspace_id, dataset_id)
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

        return await run_blocking(work)


def _register_clerk_tools(
    server: FastMCP,
    runtime: ClerkMcpRuntime,
    *,
    connector_catalog: Any | None = None,
) -> None:
    def _auth(ctx: Context | None) -> AllowlistedPrincipal | CallToolResult:
        try:
            return authenticate_bearer(
                _authorization_header(ctx),
                verifier=runtime.verifier,
                users=runtime.users,
            )
        except UnauthorizedError as error:
            return error_result(
                error=error, repair_hint="Send Authorization: Bearer <Clerk token>."
            )
        except ForbiddenError as error:
            return error_result(
                error=error,
                repair_hint="Ask the operator to allowlist your Clerk user in quail.toml.",
            )
        except AuthError as error:
            return error_result(error=error)

    def _connection_key(principal: AllowlistedPrincipal, ctx: Context | None) -> str:
        session_id = _mcp_session_id(ctx)
        if session_id is not None:
            return f"sess:{session_id}"
        return f"user:{principal.clerk_user_id}"

    def _require_workspace(
        principal: AllowlistedPrincipal,
        ctx: Context | None,
    ) -> str | CallToolResult:
        key = _connection_key(principal, ctx)
        active = runtime.sticky.ensure_initial_bind(key, principal.user)
        if active is None:
            return error_result(
                error=ValueError("No workspace bound for this connection"),
                repair_hint=UNBOUND_REPAIR_HINT,
            )
        return active

    @server.tool(title="List Quail workspaces")
    async def quail_list_workspaces(ctx: Context | None = None) -> CallToolResult:
        """List workspaces you may use and the active sticky workspace id.

        active_workspace_id is null when unbound. Locked users see only their
        default workspace. Prefer staying on the active workspace unless the
        user asks to change or the task clearly requires another.
        """

        def work() -> CallToolResult:
            principal = _auth(ctx)
            if isinstance(principal, CallToolResult):
                return principal
            key = _connection_key(principal, ctx)
            active = runtime.sticky.ensure_initial_bind(key, principal.user)
            if principal.user.lock_workspace:
                assert principal.user.default_workspace is not None
                workspaces = [{"workspace_id": principal.user.default_workspace}]
                active = principal.user.default_workspace
            else:
                workspaces = [{"workspace_id": wid} for wid in principal.user.workspaces]
            return success_result({"workspaces": workspaces, "active_workspace_id": active})

        return await run_blocking(work)

    @server.tool(title="Switch Quail workspace")
    async def quail_switch_workspace(
        workspace_id: str,
        ctx: Context | None = None,
    ) -> CallToolResult:
        """Bind this MCP connection to one allowlisted workspace.

        Sticky bind only: does not create a session. After switching, call
        quail_start_session again; do not reuse a prior session_id.
        Fails when the user is TOML-locked or the workspace is not in their
        memberships. Success returns active_workspace_id.
        """

        def work() -> CallToolResult:
            principal = _auth(ctx)
            if isinstance(principal, CallToolResult):
                return principal
            if principal.user.lock_workspace:
                return error_result(
                    error=ForbiddenError("Workspace is locked for this user"),
                    repair_hint=LOCK_REPAIR_HINT,
                )
            if workspace_id not in principal.user.workspaces:
                return error_result(
                    error=ForbiddenError(f"Not allowlisted for workspace: {workspace_id}"),
                    repair_hint="Call quail_list_workspaces and pick an allowlisted id.",
                )
            key = _connection_key(principal, ctx)
            active = runtime.sticky.bind(key, workspace_id)
            return success_result({"active_workspace_id": active})

        return await run_blocking(work)

    @server.tool(title="Get Quail API docs")
    async def quail_get_api_docs(ctx: Context | None = None) -> CallToolResult:
        """Return the analysis-language docs for code inside quail_exec."""

        def work() -> CallToolResult:
            principal = _auth(ctx)
            if isinstance(principal, CallToolResult):
                return principal
            del principal
            try:
                documentation = runtime.api_docs_path.read_text(encoding="utf-8")
            except Exception as error:
                return error_result(error=error, repair_hint="Ensure docs/api.md is readable.")
            return success_result({"documentation": documentation})

        return await run_blocking(work)

    @server.tool(title="List Quail datasets")
    async def quail_list_datasets(ctx: Context | None = None) -> CallToolResult:
        """List datasets in the active sticky workspace."""

        def work() -> CallToolResult:
            principal = _auth(ctx)
            if isinstance(principal, CallToolResult):
                return principal
            workspace_id = _require_workspace(principal, ctx)
            if isinstance(workspace_id, CallToolResult):
                return workspace_id
            try:
                with open_core_db(runtime.db_path) as db:
                    datasets = [
                        {
                            "dataset_id": ref.dataset_id,
                            "name": ref.name,
                            "active_version_id": ref.active_version_id,
                        }
                        for ref in list_datasets(db, workspace_id)
                    ]
            except Exception as error:
                return error_result(error=error)
            return success_result({"datasets": datasets})

        return await run_blocking(work)

    @server.tool(title="Start Quail session")
    async def quail_start_session(ctx: Context | None = None) -> CallToolResult:
        """Create an analysis session in the active sticky workspace.

        The session belongs to that workspace (workspace_id is returned).
        After quail_switch_workspace, start a new session; do not reuse an
        old session_id. Reuse this session_id serially on quail_exec.
        """

        def work() -> CallToolResult:
            principal = _auth(ctx)
            if isinstance(principal, CallToolResult):
                return principal
            workspace_id = _require_workspace(principal, ctx)
            if isinstance(workspace_id, CallToolResult):
                return workspace_id
            try:
                with open_core_db(runtime.db_path) as db:
                    session = create_session(db, workspace_id)
            except Exception as error:
                return error_result(error=error)
            return success_result(
                {
                    "session_id": session.id,
                    "workspace_id": session.workspace_id,
                    "state_revision": session.state_revision,
                }
            )

        return await run_blocking(work)

    @server.tool(title="Get Quail dataset info")
    async def quail_get_dataset_info(
        dataset_id: str,
        ctx: Context | None = None,
    ) -> CallToolResult:
        """Return dataset identity plus short guidance for the active workspace."""

        def work() -> CallToolResult:
            principal = _auth(ctx)
            if isinstance(principal, CallToolResult):
                return principal
            workspace_id = _require_workspace(principal, ctx)
            if isinstance(workspace_id, CallToolResult):
                return workspace_id
            try:
                with open_core_db(runtime.db_path) as db:
                    ref = get_dataset(db, workspace_id, dataset_id)
                    if ref is None:
                        raise ValueError(f"Dataset not found: {dataset_id}")
                    documentation = _dataset_documentation(
                        connector_catalog,
                        workspace_id=workspace_id,
                        user_id=principal.user.user_id,
                        dataset_id=ref.dataset_id,
                        display_name=ref.name or ref.dataset_id,
                    )
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

        return await run_blocking(work)

    @server.tool(title="Execute Quail analysis")
    async def quail_exec(
        session_id: str,
        dataset_id: str,
        code: str,
        time_window: str | None = "standard",
        ctx: Context | None = None,
    ) -> CallToolResult:
        """Run bounded Quail Python for one session and dataset in the active workspace.

        Reuse one session_id serially; do not overlap quail_exec calls on the
        same session_id. After switching workspace, start a new session first.
        """

        def work() -> CallToolResult:
            principal = _auth(ctx)
            if isinstance(principal, CallToolResult):
                return principal
            workspace_id = _require_workspace(principal, ctx)
            if isinstance(workspace_id, CallToolResult):
                return workspace_id
            try:
                validate_time_window(time_window)
                with open_core_db(runtime.db_path) as db:
                    session = require_active_session(db, session_id)
                    if session.workspace_id != workspace_id:
                        raise ValueError("Session does not belong to the active workspace")
                    outcome = exec_script(
                        db,
                        session_id=session_id,
                        dataset_id=dataset_id,
                        expected_revision=session.state_revision,
                        code=code,
                        search_runtime=runtime.search_runtime,
                        time_window=time_window,
                    )
            except Exception as error:
                return error_result(
                    error=error,
                    execution_id=None,
                    repair_hint=_exec_repair_hint(error),
                )
            return success_printed_output(outcome.printed_output)

        return await run_blocking(work)

    @server.tool(title="Provide feedback")
    async def provide_feedback(
        message: str,
        category: str | None = None,
        session_id: str | None = None,
        dataset_id: str | None = None,
        ctx: Context | None = None,
    ) -> CallToolResult:
        """Record friction notes outside the core analysis DB for the active workspace."""

        def work() -> CallToolResult:
            principal = _auth(ctx)
            if isinstance(principal, CallToolResult):
                return principal
            workspace_id = _require_workspace(principal, ctx)
            if isinstance(workspace_id, CallToolResult):
                return workspace_id
            try:
                with open_core_db(runtime.db_path) as db:
                    if session_id is not None:
                        session = get_session(db, session_id)
                        if session is None:
                            raise ValueError(f"Session not found: {session_id}")
                    if dataset_id is not None:
                        ref = get_dataset(db, workspace_id, dataset_id)
                        if ref is None:
                            raise ValueError(f"Dataset not found: {dataset_id}")
                append_feedback(
                    runtime.feedback_path,
                    workspace_id=workspace_id,
                    message=message,
                    category=category,
                    session_id=session_id,
                    dataset_id=dataset_id,
                )
            except Exception as error:
                return error_result(error=error)
            return success_result({"accepted": True})

        return await run_blocking(work)


def _authorization_header(ctx: Context | None) -> str | None:
    override = get_bearer_override()
    if override is not None:
        return override
    if ctx is None:
        return None
    try:
        request = ctx.request_context.request
    except (ValueError, AttributeError, LookupError):
        return None
    if request is None:
        return None
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    return headers.get("authorization") or headers.get("Authorization")


def _mcp_session_id(ctx: Context | None) -> str | None:
    if ctx is None:
        return None
    try:
        session: Any = ctx.session
    except (ValueError, AttributeError, LookupError):
        return None
    session_id = getattr(session, "id", None) or getattr(session, "session_id", None)
    if session_id is None:
        return None
    return str(session_id)


def _exec_repair_hint(error: BaseException) -> str | None:
    """Prefer QuailRuntimeError.repair_hint over the generic exec failure hint."""

    if isinstance(error, QuailRuntimeError) and error.repair_hint:
        return None
    return _DEFAULT_EXEC_REPAIR


def _dataset_documentation(
    connector_catalog: Any | None,
    *,
    workspace_id: str,
    user_id: str | None,
    dataset_id: str,
    display_name: str,
) -> str:
    """Resolve connector docs, else the short fallback string."""

    if connector_catalog is not None:
        from quail.connectors.load import resolve_dataset_documentation

        bundle = connector_catalog.for_workspace(workspace_id)
        document = resolve_dataset_documentation(
            bundle,
            workspace_id=workspace_id,
            user_id=user_id,
            dataset_id=dataset_id,
        )
        if document is not None and document.strip():
            return document
    return f"Dataset {display_name} ({dataset_id}). {_DATASET_INFO_FALLBACK}"
