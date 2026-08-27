"""Clerk MCP OAuth discovery (protected resource + AS metadata proxy)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

import anyio
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from quail.auth.clerk import TokenVerifier, resolve_allowlisted_user
from quail.auth.errors import ForbiddenError, UnauthorizedError
from quail.config.hosting_url import normalize_public_base_url
from quail.config.models import QuailConfig, UserSpec
from quail.mcp.offload import run_blocking

# Advertised scopes for MCP protected-resource metadata / middleware checks.
MCP_OAUTH_SCOPES: tuple[str, ...] = ("openid", "profile", "email")

_AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
_FETCH_TIMEOUT_SECONDS = 10.0
_METADATA_CACHE_TTL_SECONDS = 300.0


class _MetadataFetchError(RuntimeError):
    """Failed to proxy Clerk authorization-server metadata."""


def public_base_url_for(config: QuailConfig) -> str:
    """Resolved public origin for MCP OAuth resource metadata."""

    return config.public_base_url


def mcp_resource_url(public_base_url: str) -> str:
    """MCP streamable-http resource URL (`…/mcp`)."""

    return f"{normalize_public_base_url(public_base_url)}/mcp"


def clerk_issuer_url(clerk_domain: str) -> str:
    """Clerk authorization-server issuer from TOML `auth.clerk_domain`."""

    domain = clerk_domain.strip().removeprefix("https://").removeprefix("http://")
    domain = domain.removesuffix("/")
    if not domain:
        raise ValueError("clerk_domain cannot be empty")
    return f"https://{domain}"


def build_clerk_auth_settings(
    *,
    clerk_domain: str,
    public_base_url: str,
) -> AuthSettings:
    """MCP resource-server AuthSettings pointing at Clerk as the issuer."""

    return AuthSettings(
        issuer_url=AnyHttpUrl(clerk_issuer_url(clerk_domain)),
        resource_server_url=AnyHttpUrl(mcp_resource_url(public_base_url)),
        required_scopes=list(MCP_OAUTH_SCOPES),
    )


class ClerkAccessTokenVerifier:
    """Adapt Quail's sync TokenVerifier to the MCP SDK async TokenVerifier protocol."""

    def __init__(
        self,
        verifier: TokenVerifier,
        *,
        users: tuple[UserSpec, ...],
        scopes: tuple[str, ...] = MCP_OAUTH_SCOPES,
    ) -> None:
        self._verifier = verifier
        self._users = users
        self._scopes = scopes

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            subject = await run_blocking(lambda: self._verifier.verify(token))
            resolve_allowlisted_user(self._users, subject)
        except (UnauthorizedError, ForbiddenError):
            return None
        return AccessToken(
            token=token,
            client_id="clerk",
            scopes=list(self._scopes),
            subject=subject,
        )


def register_clerk_oauth_discovery(server: MCPServer, *, clerk_domain: str) -> None:
    """Expose AS metadata at the resource origin (proxy Clerk's discovery doc)."""

    issuer = clerk_issuer_url(clerk_domain)
    metadata_url = f"{issuer}{_AS_METADATA_PATH}"
    clerk_authorize = f"{issuer}/oauth/authorize"
    cache: dict[str, Any] = {"payload": None, "fetched_at": 0.0}
    lock = anyio.Lock()

    async def _cached_metadata() -> dict[str, Any]:
        async with lock:
            now = time.monotonic()
            payload = cache["payload"]
            if (
                isinstance(payload, dict)
                and (now - float(cache["fetched_at"])) < _METADATA_CACHE_TTL_SECONDS
            ):
                return payload
            fetched = await run_blocking(lambda: _fetch_json(metadata_url))
            cache["payload"] = fetched
            cache["fetched_at"] = time.monotonic()
            return fetched

    @server.custom_route(_AS_METADATA_PATH, methods=["GET", "OPTIONS"])
    async def oauth_authorization_server_metadata(request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                },
            )
        try:
            payload = await _cached_metadata()
        except _MetadataFetchError as error:
            return JSONResponse(
                {"error": "server_error", "error_description": str(error)},
                status_code=502,
                headers={"Access-Control-Allow-Origin": "*"},
            )
        return JSONResponse(payload, headers={"Access-Control-Allow-Origin": "*"})

    @server.custom_route("/authorize", methods=["GET"])
    async def oauth_authorize_redirect(request: Request) -> Response:
        # Some MCP clients open {resource_origin}/authorize instead of Clerk's
        # authorization_endpoint. Forward the query string to Clerk.
        query = request.url.query
        target = f"{clerk_authorize}?{query}" if query else clerk_authorize
        return RedirectResponse(url=target, status_code=302)


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise _MetadataFetchError(f"Failed to fetch OAuth metadata ({error.code})") from error
    except urllib.error.URLError as error:
        raise _MetadataFetchError("Failed to fetch OAuth metadata") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _MetadataFetchError("OAuth metadata was not JSON") from error
    if not isinstance(payload, dict):
        raise _MetadataFetchError("OAuth metadata must be a JSON object")
    return payload
