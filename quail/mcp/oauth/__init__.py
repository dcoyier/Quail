"""Public exports for ``quail.mcp.oauth``."""

from quail.config.hosting_url import default_public_base_url, normalize_public_base_url

from .oauth import (
    MCP_OAUTH_SCOPES,
    ClerkAccessTokenVerifier,
    build_clerk_auth_settings,
    clerk_issuer_url,
    mcp_resource_url,
    public_base_url_for,
    register_clerk_oauth_discovery,
)

__all__ = [
    "MCP_OAUTH_SCOPES",
    "ClerkAccessTokenVerifier",
    "build_clerk_auth_settings",
    "clerk_issuer_url",
    "default_public_base_url",
    "mcp_resource_url",
    "normalize_public_base_url",
    "public_base_url_for",
    "register_clerk_oauth_discovery",
]
