"""Register connected connector tools, resources, widgets, and HTTP routes."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import unquote

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ToolAnnotations
from starlette.requests import Request
from starlette.responses import FileResponse as StarletteFileResponse
from starlette.responses import Response

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
    resolve_workspace: Callable[[Any], tuple[str, str | None]],
) -> None:
    """Attach connector surfaces; resolver receives FastMCP Context or None."""

    providers_by_tool: dict[str, list[tuple[str, ConnectedProvider]]] = {}
    tool_extension: dict[str, str] = {}
    resource_extension: dict[str, str] = {}
    seen_resource_uris: set[str] = set()
    tool_specs: dict[str, ToolSpec] = {}
    route_owners: dict[tuple[str, str], list[tuple[str, ConnectedProvider, RouteSpec]]] = {}

    for workspace_id, bundle in catalog.by_workspace.items():
        for connected in bundle.providers:
            for tool in connected.manifest.tools:
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

    for (_extension_id, _route_id), owners in route_owners.items():
        _register_route(server, owners)


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
    resolve_workspace: Callable[[Any], tuple[str, str | None]],
) -> None:
    properties = spec.input_schema.get("properties", {})
    if not isinstance(properties, Mapping):
        properties = {}
    required = set(spec.input_schema.get("required", []))
    param_names = list(properties.keys())

    async def handler(ctx: Any = None, **kwargs: Any) -> CallToolResult:
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
                    return success_result(payload, text=result.text)
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
            + ["ctx: Any = None"]
        )
        body = ", ".join(f"{name}={name}" for name in param_names)
        glue = (
            "async def _bound(" + args + ") -> CallToolResult:\n"
            f"    return await handler(ctx, {body})\n"
        )
        namespace: dict[str, Any] = {
            "Any": Any,
            "CallToolResult": CallToolResult,
            "handler": handler,
        }
        exec(glue, namespace)
        bound = namespace["_bound"]
    else:

        async def bound(ctx: Any = None) -> CallToolResult:
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


def _register_route(
    server: FastMCP,
    owners: list[tuple[str, ConnectedProvider, RouteSpec]],
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

        def work() -> FileResponse | None:
            connected = _provider_for_workspace(
                [(owner_workspace, connected) for owner_workspace, connected, _route in owners],
                workspace_id,
            )
            if connected is None:
                return None
            context = make_request_context(
                workspace_id=workspace_id,
                user_id=None,
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
        headers: dict[str, str] = {}
        if file_result.expires_at is not None:
            remaining = max(0, file_result.expires_at - int(time.time()))
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
