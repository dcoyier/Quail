"""Public exports for ``quail.mcp.results``."""

from .results import (
    classify_exception,
    diagnostic_from_exception,
    error_result,
    success_printed_output,
    success_result,
    validate_time_window,
)

__all__ = [
    "classify_exception",
    "diagnostic_from_exception",
    "error_result",
    "success_printed_output",
    "success_result",
    "validate_time_window",
]
