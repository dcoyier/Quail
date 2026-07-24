"""MCP tool success and error result helpers."""

from __future__ import annotations

import json
import re
from typing import Any

from mcp.types import CallToolResult, TextContent

from quail.analysis.errors import (
    QuailError,
    QuailFieldError,
    QuailRuntimeError,
    QuailScopeError,
    QuailSyntaxError,
)
from quail.auth.errors import AuthError, ForbiddenError, UnauthorizedError
from quail.datasets.errors import DatasetConflictError, DatasetError, DatasetSyntaxError
from quail.session.errors import (
    SessionClosedError,
    SessionConflictError,
    SessionError,
    SessionSyntaxError,
)


_TIME_WINDOWS = frozenset({"standard", "extended"})


def success_printed_output(printed_output: str) -> CallToolResult:
    """Success payload for quail_exec."""

    return success_result({"printed_output": printed_output})


def success_result(payload: dict[str, Any]) -> CallToolResult:
    """Wrap a success dict as an MCP CallToolResult."""

    text = json.dumps(payload, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=payload,
        isError=False,
    )


def validate_time_window(time_window: str | None) -> str | None:
    """Accept standard|extended or None; ignore budgets for now."""

    if time_window is None:
        return None
    if not isinstance(time_window, str):
        raise ValueError("time_window must be a string or None")
    if time_window not in _TIME_WINDOWS:
        raise ValueError('time_window must be "standard" or "extended"')
    return time_window


def error_result(
    *,
    error: BaseException,
    execution_id: str | None = None,
    repair_hint: str | None = None,
) -> CallToolResult:
    """Build a compact MCP tool error with diagnostic."""

    diagnostic = diagnostic_from_exception(error, repair_hint=repair_hint)
    payload: dict[str, Any] = {
        "execution_id": execution_id,
        "diagnostic": diagnostic,
    }
    text = json.dumps(payload, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=payload,
        isError=True,
    )


def diagnostic_from_exception(
    error: BaseException,
    *,
    repair_hint: str | None = None,
) -> dict[str, Any]:
    error_class, stable_error_code = classify_exception(error)
    message = str(error) or error_class
    diagnostic: dict[str, Any] = {
        "error_class": error_class,
        "stable_error_code": stable_error_code,
        "message": message,
    }
    hint = repair_hint
    if hint is None and isinstance(error, QuailRuntimeError):
        hint = error.repair_hint
    if hint is not None:
        diagnostic["repair_hint"] = hint
    return diagnostic


def classify_exception(error: BaseException) -> tuple[str, str]:
    if isinstance(error, QuailSyntaxError):
        return "QuailSyntaxError", "quail_syntax_error"
    if isinstance(error, QuailScopeError):
        return "QuailScopeError", "quail_scope_error"
    if isinstance(error, QuailFieldError):
        return "QuailFieldError", "quail_field_error"
    if isinstance(error, QuailRuntimeError):
        return "QuailRuntimeError", "quail_runtime_error"
    if isinstance(error, QuailError):
        return type(error).__name__, _to_snake(type(error).__name__)
    if isinstance(error, UnauthorizedError):
        return "UnauthorizedError", "unauthorized"
    if isinstance(error, ForbiddenError):
        return "ForbiddenError", "forbidden"
    if isinstance(error, AuthError):
        return type(error).__name__, _to_snake(type(error).__name__)
    if isinstance(error, SessionSyntaxError):
        return "SessionSyntaxError", "session_syntax_error"
    if isinstance(error, SessionConflictError):
        return "SessionConflictError", "session_conflict_error"
    if isinstance(error, SessionClosedError):
        return "SessionClosedError", "session_closed_error"
    if isinstance(error, SessionError):
        return type(error).__name__, _to_snake(type(error).__name__)
    if isinstance(error, DatasetSyntaxError):
        return "DatasetSyntaxError", "dataset_syntax_error"
    if isinstance(error, DatasetConflictError):
        return "DatasetConflictError", "dataset_conflict_error"
    if isinstance(error, DatasetError):
        return type(error).__name__, _to_snake(type(error).__name__)
    if isinstance(error, ValueError):
        return "ValueError", "value_error"
    return "InternalError", "internal_error"


def _to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
