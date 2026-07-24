"""Clerk JWT verification and TOML allowlist resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import jwt
from jwt import PyJWKClient

from quail.auth.errors import ForbiddenError, UnauthorizedError
from quail.config.models import UserSpec


@dataclass(frozen=True, slots=True)
class AllowlistedPrincipal:
    """Resolved TOML user for the current bearer token."""

    user: UserSpec
    clerk_user_id: str


class TokenVerifier(Protocol):
    def verify(self, token: str) -> str:
        """Return the Clerk user id (`sub`) for a bearer token."""


class ClerkJwtVerifier:
    """Verify Clerk-issued RS256 access tokens via JWKS."""

    def __init__(self, clerk_domain: str) -> None:
        domain = clerk_domain.strip().removeprefix("https://").removesuffix("/")
        if not domain:
            raise ValueError("clerk_domain cannot be empty")
        self._issuer = f"https://{domain}"
        self._jwks = PyJWKClient(f"{self._issuer}/.well-known/jwks.json")

    def verify(self, token: str) -> str:
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub"], "verify_aud": False},
            )
        except jwt.PyJWTError as error:
            raise UnauthorizedError("Invalid bearer token") from error
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub.strip():
            raise UnauthorizedError("Bearer token missing sub")
        return sub.strip()


@dataclass(frozen=True, slots=True)
class StaticTokenVerifier:
    """Test helper: map exact bearer strings to clerk user ids."""

    tokens: dict[str, str]

    def verify(self, token: str) -> str:
        clerk_user_id = self.tokens.get(token)
        if clerk_user_id is None:
            raise UnauthorizedError("Invalid bearer token")
        return clerk_user_id


def resolve_allowlisted_user(
    users: tuple[UserSpec, ...],
    clerk_user_id: str,
) -> UserSpec:
    for user in users:
        if user.clerk_user_id == clerk_user_id:
            return user
    raise ForbiddenError("Bearer identity is not allowlisted")


def authenticate_bearer(
    authorization: str | None,
    *,
    verifier: TokenVerifier,
    users: tuple[UserSpec, ...],
) -> AllowlistedPrincipal:
    """Parse Authorization header, verify token, resolve TOML user."""

    if authorization is None or not authorization.strip():
        raise UnauthorizedError("Missing Authorization bearer token")
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("Authorization must be Bearer <token>")
    clerk_user_id = verifier.verify(token.strip())
    user = resolve_allowlisted_user(users, clerk_user_id)
    return AllowlistedPrincipal(user=user, clerk_user_id=clerk_user_id)
