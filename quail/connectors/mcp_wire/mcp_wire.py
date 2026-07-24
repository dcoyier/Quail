"""Register connected connector tools, resources, and widgets on FastMCP."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ToolAnnotations

from quail.connectors.load import (
    ConnectedProvider,
    ConnectorCatalog,
    make_request_context,
)
from quail.connectors.sdk import ConnectorError, ToolResult, ToolSpec
from quail.mcp.offload import run_blocking
from quail.mcp.results import error_result, success_result


def register_connectors(
    server: FastMCP,
    catalog: ConnectorCatalog,
    *,
    resolve_workspace: Callable[[Any], tuple[str, str | None]],
) -> None:
    """Attach connector surfaces; resolver receives FastMCP Context or None."""

    providers_by_tool: dict[str, list[tuple[str, ConnectedProvider]]] = {}
    seen_resource_uris: set[str] = set()
    tool_specs: dict[str, ToolSpec] = {}

    for workspace_id, bundle in catalog.by_workspace.items():
        for connected in bundle.providers:
            for tool in connected.manifest.tools:
                providers_by_tool.setdefault(tool.name, []).append((workspace_id, connected))
                tool_specs.setdefault(tool.name, tool)
            for resource in connected.manifest.resources:
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

    for tool_name, owners in providers_by_tool.items():
        _register_tool(server, tool_specs[tool_name], owners, resolve_workspace)


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
                    if result.text is not None:
                        payload.setdefault("text", result.text)
                    return success_result(payload)
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


def _provider_for_workspace(
    owners: list[tuple[str, ConnectedProvider]],
    workspace_id: str,
) -> ConnectedProvider | None:
    for owner_workspace, connected in owners:
        if owner_workspace == workspace_id:
            return connected
    return None
