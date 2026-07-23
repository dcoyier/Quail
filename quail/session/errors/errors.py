"""Host-facing session and overlay errors (not the agent MCP surface)."""

from __future__ import annotations


class SessionError(Exception):
    """Base failure for session and overlay host APIs."""


class SessionSyntaxError(SessionError):
    """Invalid session arguments or mutation shape."""


class SessionConflictError(SessionError):
    """Optimistic revision conflict or illegal overlay state."""


class SessionClosedError(SessionError):
    """Session is closed and cannot accept overlay commits."""
