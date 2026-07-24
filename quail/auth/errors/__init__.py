"""Public exports for ``quail.auth.errors``."""

from .errors import AuthError, ForbiddenError, UnauthorizedError

__all__ = [
    "AuthError",
    "ForbiddenError",
    "UnauthorizedError",
]
