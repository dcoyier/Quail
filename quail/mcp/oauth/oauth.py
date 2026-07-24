"""Clerk MCP OAuth discovery (protected resource + AS metadata proxy)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from quail.auth.clerk import TokenVerifier
from quail.auth.errors import UnauthorizedError
from quail.config.hosting_url import normalize_public_base_url
from quail.config.models import QuailConfig

# Advertised scopes for MCP protected-resource metadata / middleware checks.
MCP_OAUTH_SCOPES: tuple[str, ...] = ("openid", "profile", "email")

_AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
_FETCH_TIMEOUT_SECONDS = 10.0


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
        scopes: tuple[str, ...] = MCP_OAUTH_SCOPES,
    ) -> None:
        self._verifier = verifier
        self._scopes = scopes

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            subject = self._verifier.verify(token)
        except UnauthorizedError:
            return None
        return AccessToken(
            token=token,
            client_id="clerk",
            scopes=list(self._scopes),
            subject=subject,
        )


def register_clerk_oauth_discovery(server: FastMCP, *, clerk_domain: str) -> None:
    """Expose AS metadata for older MCP clients (proxy Clerk's discovery doc)."""

    issuer = clerk_issuer_url(clerk_domain)
    metadata_url = f"{issuer}{_AS_METADATA_PATH}"

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
            payload = _fetch_json(metadata_url)
        except _MetadataFetchError as error:
            return JSONResponse(
                {"error": "server_error", "error_description": str(error)},
                status_code=502,
                headers={"Access-Control-Allow-Origin": "*"},
            )
        return JSONResponse(payload, headers={"Access-Control-Allow-Origin": "*"})


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
