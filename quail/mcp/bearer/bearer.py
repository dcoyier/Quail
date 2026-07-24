"""Bearer token override for in-process MCP tool tests."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

_bearer_override: ContextVar[str | None] = ContextVar("quail_bearer_override", default=None)


def get_bearer_override() -> str | None:
    return _bearer_override.get()


@contextmanager
def bearer_token(token: str) -> Iterator[None]:
    """Set Authorization bearer for nested tool calls (tests / local inject)."""

    value = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    token_handle = _bearer_override.set(value)
    try:
        yield
    finally:
        _bearer_override.reset(token_handle)
