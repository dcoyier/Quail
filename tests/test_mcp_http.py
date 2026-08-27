"""MCP Streamable HTTP is 2026-07-28 only: no initialize handshake."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import PROTOCOL_VERSION_META_KEY, UNSUPPORTED_PROTOCOL_VERSION
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from starlette.testclient import TestClient

from quail.mcp.http import PROTOCOL_VERSION, attach_modern_http
from quail.mcp.http.http import _handshake_rejection

_TEST_TRANSPORT_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def test_protocol_version_is_the_modern_revision() -> None:
    assert PROTOCOL_VERSION == "2026-07-28"
    assert PROTOCOL_VERSION in MODERN_PROTOCOL_VERSIONS


def _app(*, streamable_http_path: str = "/mcp") -> Any:
    server = MCPServer("t")
    attach_modern_http(server)
    return server.streamable_http_app(
        streamable_http_path=streamable_http_path,
        json_response=True,
        transport_security=_TEST_TRANSPORT_SECURITY,
    )


def _assert_unsupported(response: Any, *, requested: str | None = None) -> None:
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
    assert body["error"]["data"]["supported"] == [PROTOCOL_VERSION]
    if requested is not None:
        assert body["error"]["data"]["requested"] == requested


def test_initialize_is_rejected() -> None:
    with TestClient(_app()) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
            },
        )
    _assert_unsupported(response, requested="2025-11-25")
    assert response.json()["id"] == 1


def test_initialized_notification_is_rejected() -> None:
    with TestClient(_app()) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
    _assert_unsupported(response)


def test_handshake_protocol_header_is_rejected() -> None:
    with TestClient(_app()) as client:
        response = client.post(
            "/mcp",
            headers={"mcp-protocol-version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
    _assert_unsupported(response, requested="2025-11-25")


def test_jsonrpc_without_modern_envelope_is_rejected() -> None:
    with TestClient(_app()) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )
    _assert_unsupported(response)


def _error_code(response: Any) -> object:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    return error.get("code")


def test_empty_and_non_rpc_posts_are_not_handshake_rejected() -> None:
    with TestClient(_app()) as client:
        empty = client.post("/mcp")
        blank = client.post("/mcp", content=b"{}")
    assert _error_code(empty) != UNSUPPORTED_PROTOCOL_VERSION
    assert _error_code(blank) != UNSUPPORTED_PROTOCOL_VERSION


def test_modern_header_is_not_handshake_rejected() -> None:
    with TestClient(_app()) as client:
        response = client.post(
            "/mcp",
            headers={"mcp-protocol-version": PROTOCOL_VERSION},
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        )
    if response.status_code == 400:
        assert response.json().get("error", {}).get("code") != UNSUPPORTED_PROTOCOL_VERSION


def test_modern_envelope_is_not_handshake_rejected() -> None:
    with TestClient(_app()) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/list",
                "params": {"_meta": {PROTOCOL_VERSION_META_KEY: PROTOCOL_VERSION}},
            },
        )
    if response.status_code == 400:
        assert response.json().get("error", {}).get("code") != UNSUPPORTED_PROTOCOL_VERSION


def test_initialize_is_rejected_even_with_modern_header() -> None:
    with TestClient(_app()) as client:
        response = client.post(
            "/mcp",
            headers={"mcp-protocol-version": PROTOCOL_VERSION},
            json={"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {}},
        )
    _assert_unsupported(response)


def test_custom_mcp_path_rejects_initialize() -> None:
    with TestClient(_app(streamable_http_path="/v1/mcp")) as client:
        response = client.post(
            "/v1/mcp",
            json={"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}},
        )
    _assert_unsupported(response)


def test_handshake_rejection_classifier() -> None:
    initialize = _handshake_rejection(
        header_version=None,
        body=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
    )
    assert initialize is not None

    modern = _handshake_rejection(
        header_version=PROTOCOL_VERSION,
        body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
    )
    assert modern is None

    empty = _handshake_rejection(header_version=None, body=b"")
    assert empty is None

    stale_meta = _handshake_rejection(
        header_version=None,
        body=(
            b'{"jsonrpc":"2.0","id":1,"method":"tools/list",'
            b'"params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2025-11-25"}}}'
        ),
    )
    assert stale_meta is not None
