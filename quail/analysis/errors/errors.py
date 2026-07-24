"""Public analysis errors (also used in diagnostics later)."""

from __future__ import annotations


class QuailError(Exception):
    """Base error for the analysis language."""


class QuailSyntaxError(QuailError):
    """Invalid symbolic construction, API shape, or unsupported Python."""


class QuailScopeError(QuailError):
    """Incompatible session, dataset version, group, unit, or entry scope."""


class QuailFieldError(QuailError):
    """Unknown field, kind mismatch, or source-field mutation."""


class QuailRuntimeError(QuailError):
    """Data-dependent failure, unavailable search, timeout, or resource limit."""

    def __init__(self, message: str, *, repair_hint: str | None = None) -> None:
        super().__init__(message)
        self.repair_hint = repair_hint


class QuailServerBusyError(QuailRuntimeError):
    """Process is at its concurrent quail_exec slot limit."""

    stable_error_code = "server_busy"
