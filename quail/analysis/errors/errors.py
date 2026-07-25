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


class QuailSessionBusyError(QuailRuntimeError):
    """Another quail_exec is already running for this session_id."""

    stable_error_code = "session_busy"


_WIRE_ERROR_TYPES: dict[str, type[QuailError]] = {
    "QuailSyntaxError": QuailSyntaxError,
    "QuailScopeError": QuailScopeError,
    "QuailFieldError": QuailFieldError,
    "QuailRuntimeError": QuailRuntimeError,
    "QuailServerBusyError": QuailServerBusyError,
    "QuailSessionBusyError": QuailSessionBusyError,
    "QuailError": QuailError,
}


def rehydrate_quail_error(exception_type: object, message: object) -> QuailError:
    """Rebuild a Quail error from worker/host wire fields."""

    text = str(message) if message is not None else "worker failed"
    type_name = str(exception_type) if isinstance(exception_type, str) and exception_type else ""
    if type_name:
        prefix = f"{type_name}: "
        if text.startswith(prefix):
            text = text[len(prefix) :]
    cls = _WIRE_ERROR_TYPES.get(type_name, QuailRuntimeError)
    if cls is QuailServerBusyError:
        return QuailServerBusyError(text)
    if cls is QuailSessionBusyError:
        return QuailSessionBusyError(text)
    if cls is QuailRuntimeError:
        return QuailRuntimeError(text)
    return cls(text)
