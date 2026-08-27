"""MCP 2026-07-28-only Streamable HTTP: no initialize handshake."""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from mcp_types import (
    PROTOCOL_VERSION_META_KEY,
    UNSUPPORTED_PROTOCOL_VERSION,
    ErrorData,
    JSONRPCError,
    UnsupportedProtocolVersionErrorData,
)
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

PROTOCOL_VERSION = "2026-07-28"

_HANDSHAKE_METHODS = frozenset({"initialize", "notifications/initialized"})


def attach_modern_http(server: MCPServer) -> None:
    """Force stateless Streamable HTTP and refuse handshake-era requests."""

    inner = server.streamable_http_app

    def streamable_http_app(**kwargs: Any) -> ASGIApp:
        kwargs["stateless_http"] = True
        kwargs["event_store"] = None
        path = str(kwargs.get("streamable_http_path") or "/mcp")
        return _HandshakeRejectMiddleware(inner(**kwargs), mcp_path=path)

    setattr(server, "streamable_http_app", streamable_http_app)


class _HandshakeRejectMiddleware:
    """Refuse handshake-era HTTP on the MCP path.

    Pure ASGI so Streamable HTTP SSE responses are not buffered.
    """

    def __init__(self, app: ASGIApp, *, mcp_path: str) -> None:
        self.app = app
        base = mcp_path.rstrip("/") or "/"
        self._paths = frozenset({base, f"{base}/"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in self._paths:
            method = scope.get("method")
            if method == "POST":
                body, replay = await _buffer_http_body(receive)
                header_version = _header(scope, MCP_PROTOCOL_VERSION_HEADER)
                rejected = _handshake_rejection(header_version=header_version, body=body)
                if rejected is not None:
                    await rejected(scope, replay, send)
                    return
                await self.app(_with_protocol_header(scope), replay, send)
                return
            if method != "OPTIONS":
                await Response(status_code=405, headers={"allow": "POST"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def _buffer_http_body(receive: Receive) -> tuple[bytes, Receive]:
    """Read the HTTP request body once and give back a replay receive."""

    chunks: list[bytes] = []
    leftover: MutableMapping[str, Any] | None = None
    while True:
        message = await receive()
        if message["type"] != "http.request":
            leftover = message
            break
        chunks.append(message.get("body") or b"")
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    sent_body = False
    sent_leftover = leftover is None

    async def replay() -> MutableMapping[str, Any]:
        nonlocal sent_body, sent_leftover
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        if leftover is not None and not sent_leftover:
            sent_leftover = True
            return leftover
        return await receive()

    return body, replay


def _header(scope: Scope, name: str) -> str | None:
    needle = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or ():
        if key == needle:
            return value.decode("latin-1")
    return None


def _with_protocol_header(scope: Scope) -> Scope:
    """Stamp mcp-protocol-version so the SDK cannot take the handshake entry."""

    if _header(scope, MCP_PROTOCOL_VERSION_HEADER) == PROTOCOL_VERSION:
        return scope
    needle = MCP_PROTOCOL_VERSION_HEADER.lower().encode("latin-1")
    headers = [
        (key, value) for key, value in (scope.get("headers") or ()) if key != needle
    ]
    headers.append((needle, PROTOCOL_VERSION.encode("latin-1")))
    updated = dict(scope)
    updated["headers"] = headers
    return updated


def _handshake_rejection(*, header_version: str | None, body: bytes) -> JSONResponse | None:
    """Give back a -32022 response when the POST is handshake-era, else None."""

    rpc_id, method, params = _jsonrpc_parts(body)
    if method in _HANDSHAKE_METHODS:
        requested = header_version
        if isinstance(params, dict):
            offered = params.get("protocolVersion")
            if isinstance(offered, str) and offered:
                requested = offered
        return _unsupported_response(request_id=rpc_id, requested=requested)
    if header_version == PROTOCOL_VERSION:
        return None
    if header_version is not None:
        return _unsupported_response(request_id=rpc_id, requested=header_version)
    if method is None:
        return None
    if _has_modern_envelope(params):
        return None
    return _unsupported_response(request_id=rpc_id, requested=header_version)


def _jsonrpc_parts(body: bytes) -> tuple[int | str | None, str | None, Any]:
    """Give back (id, method, params) from a JSON-RPC object, or Nones."""

    if not body.strip():
        return None, None, None
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(value, dict):
        return None, None, None
    method = value.get("method")
    if not isinstance(method, str) or not method:
        method = None
    return _jsonrpc_id(value.get("id")), method, value.get("params")


def _jsonrpc_id(value: Any) -> int | str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _has_modern_envelope(params: Any) -> bool:
    if not isinstance(params, Mapping):
        return False
    meta = params.get("_meta")
    return isinstance(meta, Mapping) and meta.get(PROTOCOL_VERSION_META_KEY) == PROTOCOL_VERSION


def _unsupported_response(*, request_id: int | str | None, requested: str | None) -> JSONResponse:
    if isinstance(requested, str) and requested:
        data: dict[str, Any] = UnsupportedProtocolVersionErrorData(
            supported=[PROTOCOL_VERSION],
            requested=requested,
        ).model_dump(mode="json")
    else:
        data = {"supported": [PROTOCOL_VERSION]}
    payload = JSONRPCError(
        jsonrpc="2.0",
        id=request_id,
        error=ErrorData(
            code=UNSUPPORTED_PROTOCOL_VERSION,
            message="Unsupported protocol version",
            data=data,
        ),
    )
    return JSONResponse(
        payload.model_dump(mode="json", by_alias=True),
        status_code=400,
    )
