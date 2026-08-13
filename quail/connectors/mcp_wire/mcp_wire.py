"""Register connected connector tools, resources, widgets, and HTTP routes."""

from __future__ import annotations

import copy
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import unquote

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, Tool, ToolAnnotations
from starlette.requests import Request
from starlette.responses import FileResponse as StarletteFileResponse
from starlette.responses import Response

from quail.auth.errors import ForbiddenError, UnauthorizedError
from quail.connectors.load import (
    ConnectedProvider,
    ConnectorCatalog,
    make_request_context,
)
from quail.connectors.sdk import (
    ConnectorError,
    FileResponse,
    RouteSpec,
    ToolResult,
    ToolSpec,
)
from quail.mcp.offload import run_blocking
from quail.mcp.results import error_result, success_result

_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Frozen host surface (unrestricted + Clerk). FastMCP overwrites duplicate
# add_tool names with a warning, so a connector must not claim these.
CORE_MCP_TOOL_NAMES = frozenset(
    {
        "quail_setup",
        "quail_get_api_docs",
        "quail_list_datasets",
        "quail_start_session",
        "quail_get_dataset_info",
        "quail_exec",
        "provide_feedback",
        "quail_list_workspaces",
        "quail_switch_workspace",
    }
)


def _claim_resource_uri(
    uri: str,
    extension_id: str,
    *,
    resource_extension: dict[str, str],
) -> None:
    owner = resource_extension.get(uri)
    if owner is not None and owner != extension_id:
        raise ConnectorError(
            "RESOURCE_URI_CONFLICT",
            f"Resource URI {uri!r} is claimed by connectors {owner!r} and {extension_id!r}.",
            "Use distinct resource URIs per connector.",
        )
    resource_extension[uri] = extension_id


def register_connectors(
    server: FastMCP,
    catalog: ConnectorCatalog,
    *,
    resolve_workspace: Callable[[Context | None], tuple[str, str | None]],
    authenticate_route: Callable[[Request, str], str | None] | None = None,
) -> None:
    """Attach connector surfaces; resolver receives FastMCP Context or None.

    authenticate_route, when set, receives the Starlette Request and URL
    workspace_id and gives back user_id (or None). Raise UnauthorizedError or
    ForbiddenError to fail the GET. Unrestricted mode omits it.
    tools/list shows connector tools only for the resolved workspace.
    """

    providers_by_tool: dict[str, list[tuple[str, ConnectedProvider]]] = {}
    tool_extension: dict[str, str] = {}
    resource_extension: dict[str, str] = {}
    seen_resource_uris: set[str] = set()
    tool_specs: dict[str, ToolSpec] = {}
    route_owners: dict[tuple[str, str], list[tuple[str, ConnectedProvider, RouteSpec]]] = {}

    for workspace_id, bundle in catalog.by_workspace.items():
        for connected in bundle.providers:
            for tool in connected.manifest.tools:
                if (
                    tool.name in CORE_MCP_TOOL_NAMES
                    or server._tool_manager.get_tool(tool.name) is not None
                ):
                    raise ConnectorError(
                        "TOOL_NAME_CONFLICT",
                        f"Tool {tool.name!r} collides with a core Quail MCP tool.",
                        "Rename the connector tool; core names such as quail_exec are reserved.",
                    )
                owner = tool_extension.get(tool.name)
                if owner is not None and owner != connected.extension_id:
                    raise ConnectorError(
                        "TOOL_NAME_CONFLICT",
                        f"Tool {tool.name!r} is claimed by connectors "
                        f"{owner!r} and {connected.extension_id!r}.",
                        "Rename one tool or remove the duplicate connector binding.",
                    )
                tool_extension[tool.name] = connected.extension_id
                providers_by_tool.setdefault(tool.name, []).append((workspace_id, connected))
                tool_specs.setdefault(tool.name, tool)
            for resource in connected.manifest.resources:
                _claim_resource_uri(
                    resource.uri,
                    connected.extension_id,
                    resource_extension=resource_extension,
                )
                if resource.uri in seen_resource_uris:
                    continue
                seen_resource_uris.add(resource.uri)
                _register_resource(
                    server,
                    connected,
                    resource.uri,
                    resource.title,
                    resource.description,
                    resource.mime_type,
                )
            for widget in connected.manifest.widgets:
                for uri in (widget.uri, *widget.compatibility_uris):
                    _claim_resource_uri(
                        uri,
                        connected.extension_id,
                        resource_extension=resource_extension,
                    )
                    if uri in seen_resource_uris:
                        continue
                    seen_resource_uris.add(uri)
                    _register_resource(
                        server,
                        connected,
                        uri,
                        title=widget.id.replace("_", " ").title(),
                        description=f"MCP UI widget from connector {connected.extension_id}",
                        mime_type="text/html;profile=mcp-app",
                        meta=dict(widget.meta) if widget.meta else None,
                    )
            for route in connected.manifest.routes:
                key = (connected.extension_id, route.id)
                route_owners.setdefault(key, []).append((workspace_id, connected, route))

    for tool_name, owners in providers_by_tool.items():
        _register_tool(server, tool_specs[tool_name], owners, resolve_workspace)

    if providers_by_tool:
        _install_workspace_scoped_list_tools(server, providers_by_tool, resolve_workspace)

    for (_extension_id, _route_id), owners in route_owners.items():
        _register_route(server, owners, authenticate_route=authenticate_route)


def _install_workspace_scoped_list_tools(
    server: FastMCP,
    providers_by_tool: dict[str, list[tuple[str, ConnectedProvider]]],
    resolve_workspace: Callable[[Context | None], tuple[str, str | None]],
) -> None:
    """Filter tools/list to connector tools bound in the active workspace.

    Core (non-connector) tools stay listed. When resolve_workspace raises
    (unbound Clerk, missing auth), omit connector tools rather than the union.
    """

    original_list_tools = server.list_tools

    async def list_tools() -> list[Tool]:
        tools = await original_list_tools()
        workspace_id: str | None = None
        try:
            workspace_id, _user_id = resolve_workspace(server.get_context())
        except Exception:
            workspace_id = None
        return _tools_visible_in_workspace(tools, providers_by_tool, workspace_id)

    server.list_tools = list_tools  # type: ignore[method-assign]
    server._mcp_server.list_tools()(list_tools)


def _tools_visible_in_workspace(
    tools: list[Tool],
    providers_by_tool: dict[str, list[tuple[str, ConnectedProvider]]],
    workspace_id: str | None,
) -> list[Tool]:
    visible: list[Tool] = []
    for tool in tools:
        owners = providers_by_tool.get(tool.name)
        if owners is None:
            visible.append(tool)
            continue
        if workspace_id is not None and _provider_for_workspace(owners, workspace_id) is not None:
            visible.append(tool)
    return visible


def _register_resource(
    server: FastMCP,
    connected: ConnectedProvider,
    uri: str,
    title: str,
    description: str,
    mime_type: str,
    meta: dict[str, Any] | None = None,
) -> None:
    connector = connected.connector
    safe_name = f"{connected.extension_id}_{abs(hash(uri))}"

    @server.resource(
        uri,
        name=safe_name,
        title=title,
        description=description,
        mime_type=mime_type,
        meta=meta,
    )
    def reader() -> str:
        return connector.read_resource(uri)


def _register_tool(
    server: FastMCP,
    spec: ToolSpec,
    owners: list[tuple[str, ConnectedProvider]],
    resolve_workspace: Callable[[Context | None], tuple[str, str | None]],
) -> None:
    properties = spec.input_schema.get("properties", {})
    if not isinstance(properties, Mapping):
        properties = {}
    required = set(spec.input_schema.get("required", []))
    param_names = list(properties.keys())

    async def handler(ctx: Context | None = None, **kwargs: Any) -> CallToolResult:
        missing = [name for name in required if name not in kwargs or kwargs[name] is None]
        if missing:
            return error_result(
                error=ConnectorError(
                    "INVALID_ARGUMENTS",
                    f"Missing required arguments: {', '.join(missing)}",
                    f"Call {spec.name} with its published input schema.",
                )
            )

        def work() -> CallToolResult:
            try:
                workspace_id, user_id = resolve_workspace(ctx)
                connected = _provider_for_workspace(owners, workspace_id)
                if connected is None:
                    raise ConnectorError(
                        "CONNECTOR_NOT_IN_WORKSPACE",
                        f"Tool {spec.name!r} is not available in workspace {workspace_id!r}.",
                        "Switch to a workspace where this connector is active, or omit the tool.",
                    )
                context = make_request_context(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    connected=connected,
                )
                result = connected.provider.call_tool(context, spec.name, kwargs)
                if isinstance(result, ToolResult):
                    payload = dict(result.structured_content)
                    return success_result(
                        payload,
                        text=result.text,
                        images=result.images,
                    )
                if isinstance(result, dict):
                    return success_result(dict(result))
                raise ConnectorError(
                    "INVALID_TOOL_RESULT",
                    "Connector tool returned an unsupported result type.",
                    "Return ToolResult or a JSON object.",
                )
            except Exception as error:
                return error_result(error=error)

        return await run_blocking(work)

    if param_names:
        required_params = [name for name in param_names if name in required]
        optional_params = [name for name in param_names if name not in required]
        args = ", ".join(
            [f"{name}: Any" for name in required_params]
            + [f"{name}: Any = None" for name in optional_params]
            + ["ctx: Context | None = None"]
        )
        body = ", ".join(f"{name}={name}" for name in param_names)
        glue = (
            "async def _bound(" + args + ") -> CallToolResult:\n"
            f"    return await handler(ctx, {body})\n"
        )
        namespace: dict[str, Any] = {
            "Any": Any,
            "Context": Context,
            "CallToolResult": CallToolResult,
            "handler": handler,
        }
        exec(glue, namespace)
        bound = namespace["_bound"]
    else:

        async def bound(ctx: Context | None = None) -> CallToolResult:
            return await handler(ctx)

    server.add_tool(
        bound,
        name=spec.name,
        title=spec.title,
        description=spec.description,
        annotations=ToolAnnotations(
            readOnlyHint=spec.read_only,
            destructiveHint=spec.destructive,
            idempotentHint=spec.idempotent,
            openWorldHint=spec.open_world,
        ),
        meta=dict(spec.meta) if spec.meta else None,
    )
    # FastMCP derives tools/list schema from the Python signature (Any → useless).
    # Publish ToolSpec.input_schema so agents see the real contract.
    registered = server._tool_manager.get_tool(spec.name)
    if registered is None:
        raise ConnectorError(
            "TOOL_REGISTRATION_FAILED",
            f"FastMCP did not retain tool {spec.name!r} after add_tool.",
            "Retry server startup; if it persists, report a Quail wire bug.",
        )
    registered.parameters = _published_input_schema(spec)


def _published_input_schema(spec: ToolSpec) -> dict[str, Any]:
    """Plain JSON-object copy of ToolSpec.input_schema for FastMCP Tool.parameters."""

    return copy.deepcopy(dict(spec.input_schema))


def _register_route(
    server: FastMCP,
    owners: list[tuple[str, ConnectedProvider, RouteSpec]],
    *,
    authenticate_route: Callable[[Request, str], str | None] | None = None,
) -> None:
    sample = owners[0][2]
    extension_id = owners[0][1].extension_id
    route_id = sample.id
    path = f"/extensions/{extension_id}/{{workspace_id}}/{sample.path_suffix}"
    param_names = _PARAM.findall(sample.path_suffix)

    @server.custom_route(path, methods=["GET"], name=f"connector_{extension_id}_{route_id}")
    async def route_handler(request: Request) -> Response:
        workspace_id = unquote(request.path_params.get("workspace_id", ""))
        path_params = {
            name: unquote(str(request.path_params.get(name, ""))) for name in param_names
        }
        path_params["workspace_id"] = workspace_id
        user_id: str | None = None
        if authenticate_route is not None:
            try:
                user_id = authenticate_route(request, workspace_id)
            except UnauthorizedError:
                return Response(status_code=401)
            except ForbiddenError:
                return Response(status_code=403)

        def work() -> FileResponse | None:
            connected = _provider_for_workspace(
                [(owner_workspace, connected) for owner_workspace, connected, _route in owners],
                workspace_id,
            )
            if connected is None:
                return None
            context = make_request_context(
                workspace_id=workspace_id,
                user_id=user_id,
                connected=connected,
            )
            handle = getattr(connected.provider, "handle_route", None)
            if handle is None:
                return None
            result = handle(context, route_id, path_params)
            if result is None:
                return None
            if not isinstance(result, FileResponse):
                raise ConnectorError(
                    "INVALID_ROUTE_RESULT",
                    "Connector route returned an unsupported result type.",
                    "Return FileResponse or None.",
                )
            size = result.path.stat().st_size
            if size > sample.max_body_bytes:
                raise ConnectorError(
                    "ASSET_TOO_LARGE",
                    "The connector file exceeds the route body limit.",
                    "Ask the operator to raise max_body_bytes or shrink the file.",
                )
            return result

        try:
            file_result = await run_blocking(work)
        except Exception:
            return Response(status_code=500)
        if file_result is None:
            return Response(status_code=404)
        now = int(time.time())
        if file_result.expires_at is not None and file_result.expires_at < now:
            return Response(status_code=404)
        headers: dict[str, str] = {}
        if file_result.expires_at is not None:
            remaining = max(0, file_result.expires_at - now)
            headers["Cache-Control"] = f"private, max-age={remaining}"
        return StarletteFileResponse(
            path=file_result.path,
            media_type=file_result.content_type,
            filename=file_result.filename,
            headers=headers,
        )


def _provider_for_workspace(
    owners: list[tuple[str, ConnectedProvider]],
    workspace_id: str,
) -> ConnectedProvider | None:
    for owner_workspace, connected in owners:
        if owner_workspace == workspace_id:
            return connected
    return None
