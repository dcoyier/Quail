"""Auth errors for Clerk verification and allowlist resolution."""

from __future__ import annotations


class AuthError(Exception):
    """Base auth failure."""


class UnauthorizedError(AuthError):
    """Missing or invalid bearer token."""


class ForbiddenError(AuthError):
    """Valid token but not allowlisted or not permitted."""
