"""Clerk MCP OAuth discovery and public_base_url wiring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import UNSUPPORTED_PROTOCOL_VERSION
from starlette.testclient import TestClient

from quail.auth import StaticTokenVerifier, UnauthorizedError
from quail.config import ConfigError, load_config
from quail.config.models import UserSpec
from quail.config.hosting_url import (
    default_public_base_url,
    is_loopback_host,
    is_loopback_public_base_url,
    normalize_public_base_url,
)
from quail.mcp import create_mcp_server_from_config
from quail.mcp.oauth import (
    MCP_OAUTH_SCOPES,
    ClerkAccessTokenVerifier,
    build_clerk_auth_settings,
    clerk_issuer_url,
    mcp_resource_url,
)
from quail.run import apply_config

_TEST_TRANSPORT_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _streamable_app(server: Any):
    return server.streamable_http_app(transport_security=_TEST_TRANSPORT_SECURITY)


def _write_clerk_manifest(
    tmp_path: Path, *, hosting_extra: str = "", bind: str = "127.0.0.1"
) -> Path:
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
clerk_authorized_parties = ["test_clerk_app"]

[hosting]
bind = "{bind}"
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
    assert default_public_base_url(bind="127.0.0.1", port=8000) == "http://127.0.0.1:8000"
    assert default_public_base_url(bind="::1", port=8000) == "http://[::1]:8000"
    assert default_public_base_url(bind="[::1]", port=8000) == "http://[::1]:8000"
    with pytest.raises(ValueError, match="wildcard bind"):
        default_public_base_url(bind="0.0.0.0", port=8000)
    with pytest.raises(ValueError, match="wildcard bind"):
        default_public_base_url(bind="::", port=8000)
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("LOCALHOST")
    assert is_loopback_host("[::1]")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert is_loopback_public_base_url("http://127.0.0.1:8000")
    assert is_loopback_public_base_url("http://[::1]:8000")
    assert not is_loopback_public_base_url("https://tunnel.example")
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


def test_wildcard_bind_requires_explicit_public_base_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="public_base_url is required"):
        load_config(_write_clerk_manifest(tmp_path, bind="0.0.0.0"))
    with pytest.raises(ConfigError, match="public_base_url is required"):
        load_config(_write_clerk_manifest(tmp_path, bind="::"))
    config = load_config(
        _write_clerk_manifest(
            tmp_path,
            bind="0.0.0.0",
            hosting_extra='public_base_url = "https://acme.example"\n',
        )
    )
    assert config.bind == "0.0.0.0"
    assert config.public_base_url == "https://acme.example"


def test_unbracketed_ipv6_loopback_default_public_base_url(tmp_path: Path) -> None:
    config = load_config(_write_clerk_manifest(tmp_path, bind="::1"))
    assert config.bind == "::1"
    assert config.public_base_url == "http://[::1]:8765"


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

    users = (
        UserSpec(
            user_id="alice",
            clerk_user_id="user_alice",
            workspaces=("acme",),
            default_workspace="acme",
            lock_workspace=False,
        ),
    )
    verifier = ClerkAccessTokenVerifier(
        StaticTokenVerifier({"tok": "user_alice", "eve": "user_eve"}),
        users=users,
    )

    async def _run() -> None:
        access = await verifier.verify_token("tok")
        assert access is not None
        assert access.subject == "user_alice"
        assert access.scopes == list(MCP_OAUTH_SCOPES)
        assert await verifier.verify_token("nope") is None
        assert await verifier.verify_token("eve") is None

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
    ).server
    app = _streamable_app(server)
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


def test_clerk_mcp_rejects_initialize(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(
        tmp_path,
        hosting_extra='public_base_url = "https://acme.example"\n',
    )
    config = load_config(manifest)
    apply_config(config)
    server = create_mcp_server_from_config(
        config,
        verifier=StaticTokenVerifier({"alice-token": "user_alice"}),
    ).server
    app = _streamable_app(server)
    with TestClient(app) as client:
        handshake = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
            },
        )
        denied = client.post("/mcp")
        get = client.get("/mcp")
    assert handshake.status_code == 400
    body = handshake.json()
    assert body["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
    assert body["error"]["data"]["supported"] == ["2026-07-28"]
    assert denied.status_code == 401
    assert get.status_code == 405


def test_mcp_rejects_valid_token_for_unknown_user(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(
        tmp_path,
        hosting_extra='public_base_url = "https://acme.example"\n',
    )
    config = load_config(manifest)
    apply_config(config)
    server = create_mcp_server_from_config(
        config,
        verifier=StaticTokenVerifier({"alice-token": "user_alice", "eve-token": "user_eve"}),
    ).server
    app = _streamable_app(server)
    with TestClient(app) as client:
        denied = client.post("/mcp", headers={"Authorization": "Bearer eve-token"})
        assert denied.status_code == 401
        allowed = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer alice-token",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            content=b"{}",
        )
        assert allowed.status_code != 401


def test_authorization_server_metadata_proxy(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path)
    config = load_config(manifest)
    apply_config(config)
    server = create_mcp_server_from_config(
        config,
        verifier=StaticTokenVerifier({"alice-token": "user_alice"}),
    ).server
    app = _streamable_app(server)
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


def test_authorize_redirects_to_clerk(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path)
    config = load_config(manifest)
    apply_config(config)
    server = create_mcp_server_from_config(
        config,
        verifier=StaticTokenVerifier({"alice-token": "user_alice"}),
    ).server
    app = _streamable_app(server)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": "test_clerk_app",
                "redirect_uri": "https://client.example/callback",
            },
        )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://example.clerk.accounts.dev/oauth/authorize?")
    assert "response_type=code" in location
    assert "client_id=test_clerk_app" in location
    assert "redirect_uri=" in location


def test_userinfo_fallback_on_clerk_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from quail.auth.clerk import ClerkJwtVerifier

    verifier = ClerkJwtVerifier(
        "example.clerk.accounts.dev",
        authorized_parties=("test_clerk_app",),
    )
    userinfo_calls: list[str] = []

    def _fake_urlopen(request: Any, timeout: float = 0) -> Any:
        class _Resp:
            def read(self) -> bytes:
                return json.dumps({"sub": "user_from_info"}).encode("utf-8")

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        assert request.get_header("Authorization") == "Bearer opaque-token"
        userinfo_calls.append("called")
        return _Resp()

    def _jwt_should_not_run(_token: str) -> str:
        raise AssertionError("JWT path must not run for opaque tokens")

    monkeypatch.setattr(verifier, "_verify_jwt", _jwt_should_not_run)
    with patch("quail.auth.clerk.clerk.urllib.request.urlopen", _fake_urlopen):
        assert verifier.verify("opaque-token") == "user_from_info"
    assert userinfo_calls == ["called"]


def test_wrong_party_jwt_does_not_fall_back_to_userinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from quail.auth.clerk import ClerkJwtVerifier

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "user_alice",
            "iss": "https://example.clerk.accounts.dev",
            "azp": "wrong_app",
            "exp": now + 3600,
            "iat": now,
        },
        private_key,
        algorithm="RS256",
    )
    verifier = ClerkJwtVerifier(
        "example.clerk.accounts.dev",
        authorized_parties=("test_clerk_app",),
    )

    class _FakeKey:
        def __init__(self, key: object) -> None:
            self.key = key

    monkeypatch.setattr(
        verifier._jwks,
        "get_signing_key_from_jwt",
        lambda _token: _FakeKey(public_key),
    )

    def _fake_urlopen(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("userinfo must not be called for JWT party failures")

    with patch("quail.auth.clerk.clerk.urllib.request.urlopen", _fake_urlopen):
        with pytest.raises(UnauthorizedError, match="not for this Quail application"):
            verifier.verify(token)


def test_jwt_shaped_garbage_does_not_fall_back_to_userinfo() -> None:
    from quail.auth.clerk import ClerkJwtVerifier

    verifier = ClerkJwtVerifier(
        "example.clerk.accounts.dev",
        authorized_parties=("test_clerk_app",),
    )
    # Valid base64url JSON header + two more segments → JWT-shaped.
    token = "eyJhbGciOiJub25lIn0.e30.x"

    def _fake_urlopen(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("userinfo must not be called for JWT-shaped tokens")

    with patch("quail.auth.clerk.clerk.urllib.request.urlopen", _fake_urlopen):
        with pytest.raises(UnauthorizedError, match="Invalid bearer token"):
            verifier.verify(token)


def test_clerk_rejects_non_loopback_http_public_base_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="allow_insecure_http|https"):
        load_config(
            _write_clerk_manifest(
                tmp_path,
                hosting_extra='public_base_url = "http://public.example"\n',
            )
        )


def test_clerk_allows_insecure_http_with_override(tmp_path: Path) -> None:
    config = load_config(
        _write_clerk_manifest(
            tmp_path,
            hosting_extra=(
                'public_base_url = "http://public.example"\nallow_insecure_http = true\n'
            ),
        )
    )
    assert config.public_base_url == "http://public.example"
    assert config.allow_insecure_http is True


def test_access_token_verifier_offloads_sync_verify() -> None:
    import asyncio
    import threading

    class _SlowVerifier:
        def __init__(self) -> None:
            self.thread_ids: list[int] = []

        def verify(self, token: str) -> str:
            self.thread_ids.append(threading.get_ident())
            return f"user_{token}"

    async def _run() -> None:
        slow = _SlowVerifier()
        adapter = ClerkAccessTokenVerifier(
            slow,
            users=(
                UserSpec(
                    user_id="abc",
                    clerk_user_id="user_abc",
                    workspaces=("acme",),
                    default_workspace="acme",
                    lock_workspace=False,
                ),
            ),
        )
        main_thread = threading.get_ident()
        result = await adapter.verify_token("abc")
        assert result is not None
        assert result.subject == "user_abc"
        assert slow.thread_ids
        assert slow.thread_ids[0] != main_thread
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.5)

    asyncio.run(_run())


def test_oauth_metadata_served_from_cache(tmp_path: Path) -> None:
    config = load_config(
        _write_clerk_manifest(
            tmp_path,
            hosting_extra='public_base_url = "https://acme.example"\n',
        )
    )
    apply_config(config)
    calls: list[str] = []

    def _fake_fetch(url: str) -> dict[str, Any]:
        calls.append(url)
        return {
            "issuer": "https://example.clerk.accounts.dev",
            "authorization_endpoint": "https://example.clerk.accounts.dev/oauth/authorize",
        }

    with patch("quail.mcp.oauth.oauth._fetch_json", _fake_fetch):
        server = create_mcp_server_from_config(
            config,
            verifier=StaticTokenVerifier({"tok": "user_alice"}),
        ).server
        app = _streamable_app(server)
        with TestClient(app) as client:
            first = client.get("/.well-known/oauth-authorization-server")
            second = client.get("/.well-known/oauth-authorization-server")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["issuer"] == "https://example.clerk.accounts.dev"
    assert len(calls) == 1


def test_jwt_verify_via_local_jwks_http_round_trip() -> None:
    """RSA-signed JWT verified through a real PyJWKClient HTTP JWKS fetch."""

    import json
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt import PyJWKClient
    from jwt.algorithms import RSAAlgorithm

    from quail.auth.clerk import ClerkJwtVerifier

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = "test-key"
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    jwks_body = json.dumps({"keys": [jwk]}).encode("utf-8")

    class _JwksHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/.well-known/jwks.json":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(jwks_body)))
            self.end_headers()
            self.wfile.write(jwks_body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            del format, args

    server = HTTPServer(("127.0.0.1", 0), _JwksHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        jwks_url = f"http://127.0.0.1:{port}/.well-known/jwks.json"
        verifier = ClerkJwtVerifier(
            "example.clerk.accounts.dev",
            authorized_parties=("test_clerk_app",),
        )
        # Issuer stays Clerk-shaped; only the JWKS client points at the local fixture.
        verifier._jwks = PyJWKClient(jwks_url)

        now = int(time.time())
        good = jwt.encode(
            {
                "sub": "user_alice",
                "iss": "https://example.clerk.accounts.dev",
                "azp": "test_clerk_app",
                "exp": now + 3600,
                "iat": now,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        assert verifier.verify(good) == "user_alice"

        wrong_party = jwt.encode(
            {
                "sub": "user_alice",
                "iss": "https://example.clerk.accounts.dev",
                "azp": "other_app",
                "exp": now + 3600,
                "iat": now,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        with pytest.raises(UnauthorizedError, match="not for this Quail application"):
            verifier.verify(wrong_party)
    finally:
        server.shutdown()
        server.server_close()
