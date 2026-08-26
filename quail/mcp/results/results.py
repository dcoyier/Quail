"""MCP tool success and error result helpers."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from typing import Any

from mcp.types import CallToolResult, ContentBlock, ImageContent, TextContent

from quail.analysis.errors import (
    QuailError,
    QuailFieldError,
    QuailRuntimeError,
    QuailScopeError,
    QuailServerBusyError,
    QuailSessionBusyError,
    QuailSyntaxError,
)
from quail.auth.errors import AuthError, ForbiddenError, UnauthorizedError
from quail.connectors.sdk import ConnectorError, ToolImage
from quail.datasets.errors import DatasetConflictError, DatasetError, DatasetSyntaxError
from quail.session.errors import (
    SessionClosedError,
    SessionConflictError,
    SessionError,
    SessionSyntaxError,
)


def success_printed_output(printed_output: str) -> CallToolResult:
    """Success payload for quail_exec."""

    return success_result({"printed_output": printed_output})


def success_result(
    payload: dict[str, Any],
    *,
    text: str | None = None,
    images: Sequence[ToolImage] = (),
) -> CallToolResult:
    """Wrap a success dict as an MCP CallToolResult.

    When ``text`` is set, it becomes the human-readable content block.
    When ``text`` is omitted and there are no images, the payload is
    JSON-serialized into a text content block.
    When ``text`` is omitted and ``images`` is non-empty, content is
    image-only (no text block).
    ``structured_content`` is always the payload alone (never merged with text).
    Optional ``images`` become MCP ``ImageContent`` blocks after any text block.
    """

    content: list[ContentBlock] = []
    if text is not None:
        content.append(TextContent(type="text", text=text))
    elif not images:
        content.append(
            TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
        )
    for image in images:
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(image.data).decode("ascii"),
                mime_type=image.mime_type,
            )
        )
    return CallToolResult(
        content=content,
        structured_content=payload,
        is_error=False,
    )

def validate_time_window(time_window: str | None) -> str:
    """Accept standard|extended or None (treated as standard)."""

    from quail.analysis.limits import validate_time_window as _validate

    return _validate(time_window)


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
        structured_content=payload,
        is_error=True,
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
    if hint is None and isinstance(error, ConnectorError):
        hint = error.repair_hint
    if hint is not None:
        diagnostic["repair_hint"] = hint
    return diagnostic


def classify_exception(error: BaseException) -> tuple[str, str]:
    if isinstance(error, ConnectorError):
        return "ConnectorError", error.stable_code.casefold()
    if isinstance(error, QuailSyntaxError):
        return "QuailSyntaxError", "quail_syntax_error"
    if isinstance(error, QuailScopeError):
        return "QuailScopeError", "quail_scope_error"
    if isinstance(error, QuailFieldError):
        return "QuailFieldError", "quail_field_error"
    if isinstance(error, QuailServerBusyError):
        return "QuailRuntimeError", QuailServerBusyError.stable_error_code
    if isinstance(error, QuailSessionBusyError):
        return "QuailRuntimeError", QuailSessionBusyError.stable_error_code
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
        # Agent-facing diagnostics use the api.md Quail* table; session/dataset
        # resolve failures are scope pairing problems.
        return "QuailScopeError", "quail_scope_error"
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
        return "QuailSyntaxError", "quail_syntax_error"
    return "InternalError", "internal_error"


def _to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
