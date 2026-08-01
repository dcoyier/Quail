"""Clerk JWT / OAuth token verification and TOML allowlist resolution."""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

import jwt
from jwt import PyJWKClient

from quail.auth.errors import ForbiddenError, UnauthorizedError
from quail.config.models import UserSpec

_USERINFO_TIMEOUT_SECONDS = 10.0
_LOG = logging.getLogger("quail.auth.clerk")
if not _LOG.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    _LOG.addHandler(_handler)
    _LOG.setLevel(logging.WARNING)
    _LOG.propagate = True
_PEEK_CLAIM_KEYS = ("iss", "aud", "azp", "sub", "client_id", "exp", "iat", "nbf", "scope")


@dataclass(frozen=True, slots=True)
class AllowlistedPrincipal:
    """Resolved TOML user for the current bearer token."""

    user: UserSpec
    clerk_user_id: str


class TokenVerifier(Protocol):
    def verify(self, token: str) -> str:
        """Return the Clerk user id (`sub`) for a bearer token."""


class ClerkJwtVerifier:
    """Verify Clerk-issued tokens via JWKS JWT, with OAuth userinfo fallback.

    Identity mode: signature + issuer + sub, then TOML allowlist (elsewhere).
    App binding: JWT must present azp or aud in authorized_parties. Opaque
    (non-JWT-shaped) tokens verified only via userinfo skip that claim check
    (Clerk issuer still vouches for the token). JWT-shaped tokens never fall
    back to userinfo after any JWT failure, including party mismatch.
    """

    def __init__(
        self,
        clerk_domain: str,
        *,
        authorized_parties: tuple[str, ...] = (),
    ) -> None:
        domain = clerk_domain.strip().removeprefix("https://").removesuffix("/")
        if not domain:
            raise ValueError("clerk_domain cannot be empty")
        parties = tuple(party.strip() for party in authorized_parties if party.strip())
        if not parties:
            raise ValueError("authorized_parties must be a non-empty tuple of party ids")
        self._issuer = f"https://{domain}"
        self._jwks = PyJWKClient(f"{self._issuer}/.well-known/jwks.json")
        self._userinfo_url = f"{self._issuer}/oauth/userinfo"
        self._authorized_parties = frozenset(parties)

    def verify(self, token: str) -> str:
        if _is_jwt_shaped(token):
            try:
                return self._verify_jwt(token)
            except UnauthorizedError as jwt_error:
                _LOG.debug(
                    "clerk jwt verify failed: %s; peek=%s",
                    jwt_error,
                    _peek_token_claims(token),
                )
                raise
        try:
            return self._verify_userinfo(token)
        except UnauthorizedError as userinfo_error:
            _LOG.debug("clerk userinfo verify failed: %s", userinfo_error)
            raise

    def _verify_jwt(self, token: str) -> str:
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
            raise UnauthorizedError(f"Invalid bearer token ({type(error).__name__})") from error
        _require_authorized_party(payload, self._authorized_parties)
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub.strip():
            raise UnauthorizedError("Bearer token missing sub")
        return sub.strip()

    def _verify_userinfo(self, token: str) -> str:
        request = urllib.request.Request(
            self._userinfo_url,
            method="GET",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_USERINFO_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise UnauthorizedError(
                f"Invalid bearer token (userinfo HTTP {error.code})"
            ) from error
        except urllib.error.URLError as error:
            raise UnauthorizedError("Invalid bearer token (userinfo unreachable)") from error
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UnauthorizedError("Invalid bearer token") from error
        if not isinstance(payload, dict):
            raise UnauthorizedError("Invalid bearer token")
        sub = payload.get("sub")
        if isinstance(sub, str) and sub.strip():
            return sub.strip()
        raise UnauthorizedError("Bearer token missing sub")


def _is_jwt_shaped(token: str) -> bool:
    """True when the token has three segments and a base64url JSON object header."""

    parts = token.split(".")
    if len(parts) != 3 or not parts[0]:
        return False
    try:
        padded = parts[0] + "=" * (-len(parts[0]) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        header = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(header, dict)


def _peek_token_claims(token: str) -> dict[str, object]:
    """Unverified claim peek for auth diagnostics (no email/profile fields)."""

    parts = token.count(".")
    if parts != 2:
        return {"jwt_parts": parts + 1, "token_len": len(token)}
    try:
        payload = jwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False, "verify_nbf": False},
        )
    except jwt.PyJWTError as error:
        return {"peek_error": type(error).__name__, "token_len": len(token)}
    if not isinstance(payload, dict):
        return {"peek_error": "non_object_payload"}
    out: dict[str, object] = {}
    for key in _PEEK_CLAIM_KEYS:
        if key in payload:
            out[key] = payload[key]
    return out


def _require_authorized_party(payload: dict[str, object], parties: frozenset[str]) -> None:
    """Require JWT azp, aud, or client_id to match a configured Clerk party."""

    candidates: list[str] = []
    for key in ("azp", "client_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    aud = payload.get("aud")
    if isinstance(aud, str) and aud.strip():
        candidates.append(aud.strip())
    elif isinstance(aud, list):
        for item in aud:
            if isinstance(item, str) and item.strip():
                candidates.append(item.strip())
    if not candidates:
        raise UnauthorizedError("Bearer token missing authorized party (azp/aud/client_id)")
    if not parties.intersection(candidates):
        raise UnauthorizedError(
            "Bearer token is not for this Quail application "
            f"(candidates={sorted(set(candidates))})"
        )


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
