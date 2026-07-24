"""Clerk MCP OAuth discovery and public_base_url wiring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from quail.auth import StaticTokenVerifier, UnauthorizedError
from quail.config import ConfigError, load_config
from quail.config.hosting_url import default_public_base_url, normalize_public_base_url
from quail.mcp import create_mcp_server_from_config
from quail.mcp.oauth import (
    MCP_OAUTH_SCOPES,
    ClerkAccessTokenVerifier,
    build_clerk_auth_settings,
    clerk_issuer_url,
    mcp_resource_url,
)
from quail.run import apply_config


def _write_clerk_manifest(tmp_path: Path, *, hosting_extra: str = "") -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "notes.csv").write_text("id,title\ne1,Hello\n", encoding="utf-8")
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        f"""
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"

[auth]
mode = "clerk"
clerk_domain = "example.clerk.accounts.dev"

[hosting]
bind = "127.0.0.1"
port = 8765
{hosting_extra}
[[workspaces]]
id = "acme"

[[workspaces.datasets]]
id = "notes"
source = "data/notes.csv"

[[users]]
id = "alice"
clerk_user_id = "user_alice"
workspaces = ["acme"]
default_workspace = "acme"
""",
        encoding="utf-8",
    )
    return manifest


def test_normalize_and_default_public_base_url() -> None:
    assert normalize_public_base_url("https://tunnel.example/") == "https://tunnel.example"
    assert default_public_base_url(bind="0.0.0.0", port=8000) == "http://127.0.0.1:8000"
    with pytest.raises(ValueError, match="origin only"):
        normalize_public_base_url("https://tunnel.example/mcp")


def test_public_base_url_defaults_and_override(tmp_path: Path) -> None:
    config = load_config(_write_clerk_manifest(tmp_path))
    assert config.public_base_url == "http://127.0.0.1:8765"

    config = load_config(
        _write_clerk_manifest(
            tmp_path,
            hosting_extra='public_base_url = "https://acme.ngrok.app"\n',
        )
    )
    assert config.public_base_url == "https://acme.ngrok.app"


def test_public_base_url_rejects_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="public_base_url"):
        load_config(
            _write_clerk_manifest(
                tmp_path,
                hosting_extra='public_base_url = "https://acme.ngrok.app/mcp"\n',
            )
        )


def test_build_clerk_auth_settings() -> None:
    settings = build_clerk_auth_settings(
        clerk_domain="example.clerk.accounts.dev",
        public_base_url="https://acme.example",
    )
    assert str(settings.issuer_url).rstrip("/") == "https://example.clerk.accounts.dev"
    assert str(settings.resource_server_url).rstrip("/") == "https://acme.example/mcp"
    assert settings.required_scopes == list(MCP_OAUTH_SCOPES)
    assert clerk_issuer_url("https://example.clerk.accounts.dev/") == (
        "https://example.clerk.accounts.dev"
    )
    assert mcp_resource_url("https://acme.example") == "https://acme.example/mcp"


def test_clerk_access_token_verifier() -> None:
    import asyncio

    verifier = ClerkAccessTokenVerifier(StaticTokenVerifier({"tok": "user_alice"}))

    async def _run() -> None:
        access = await verifier.verify_token("tok")
        assert access is not None
        assert access.subject == "user_alice"
        assert access.scopes == list(MCP_OAUTH_SCOPES)
        assert await verifier.verify_token("nope") is None

    asyncio.run(_run())


def test_protected_resource_metadata_and_mcp_401(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(
        tmp_path,
        hosting_extra='public_base_url = "https://acme.example"\n',
    )
    config = load_config(manifest)
    apply_config(config)
    server = create_mcp_server_from_config(
        config,
        verifier=StaticTokenVerifier({"alice-token": "user_alice"}),
    )
    app = server.streamable_http_app()
    with TestClient(app) as client:
        meta = client.get("/.well-known/oauth-protected-resource/mcp")
        assert meta.status_code == 200
        body = meta.json()
        assert body["resource"].rstrip("/") == "https://acme.example/mcp"
        assert [url.rstrip("/") for url in body["authorization_servers"]] == [
            "https://example.clerk.accounts.dev"
        ]
        assert body["scopes_supported"] == list(MCP_OAUTH_SCOPES)

        denied = client.post("/mcp")
        assert denied.status_code == 401
        www = denied.headers.get("www-authenticate", "")
        assert "resource_metadata=" in www
        assert "oauth-protected-resource/mcp" in www


def test_authorization_server_metadata_proxy(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path)
    config = load_config(manifest)
    apply_config(config)
    server = create_mcp_server_from_config(
        config,
        verifier=StaticTokenVerifier({"alice-token": "user_alice"}),
    )
    app = server.streamable_http_app()
    payload = {
        "issuer": "https://example.clerk.accounts.dev",
        "authorization_endpoint": "https://example.clerk.accounts.dev/oauth/authorize",
        "token_endpoint": "https://example.clerk.accounts.dev/oauth/token",
    }

    def _fake_urlopen(request: Any, timeout: float = 0) -> Any:
        class _Resp:
            def read(self) -> bytes:
                return json.dumps(payload).encode("utf-8")

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        assert "oauth-authorization-server" in request.full_url
        return _Resp()

    with (
        patch("quail.mcp.oauth.oauth.urllib.request.urlopen", _fake_urlopen),
        TestClient(app) as client,
    ):
        response = client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        assert response.json()["issuer"] == payload["issuer"]


def test_userinfo_fallback_on_clerk_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from quail.auth.clerk import ClerkJwtVerifier

    verifier = ClerkJwtVerifier("example.clerk.accounts.dev")

    def _boom(_token: str) -> str:
        raise UnauthorizedError("Invalid bearer token")

    monkeypatch.setattr(verifier, "_verify_jwt", _boom)

    def _fake_urlopen(request: Any, timeout: float = 0) -> Any:
        class _Resp:
            def read(self) -> bytes:
                return json.dumps({"user_id": "user_from_info"}).encode("utf-8")

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        assert request.get_header("Authorization") == "Bearer opaque-token"
        return _Resp()

    with patch("quail.auth.clerk.clerk.urllib.request.urlopen", _fake_urlopen):
        assert verifier.verify("opaque-token") == "user_from_info"
