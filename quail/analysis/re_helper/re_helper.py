"""Restricted regex helper injected as `re` (not Python's re module)."""

from __future__ import annotations

import re as python_re

from quail.analysis.errors import QuailSyntaxError

ALLOWED_REGEX_FLAGS = int(python_re.I | python_re.M | python_re.S | python_re.A | python_re.U)
MAX_REGEX_PATTERN_BYTES = 16 * 1024


class ReFacade:
    NOFLAG = int(python_re.NOFLAG)
    A = ASCII = int(python_re.A)
    I = IGNORECASE = int(python_re.I)  # noqa: E741 — mirrors re.I
    M = MULTILINE = int(python_re.M)
    S = DOTALL = int(python_re.S)
    U = UNICODE = int(python_re.U)

    def escape(self, pattern: str) -> str:
        if not isinstance(pattern, str):
            raise QuailSyntaxError("re.escape(pattern) requires text")
        return python_re.escape(pattern)


def validate_regex_flags(flags: int) -> None:
    if isinstance(flags, bool) or not isinstance(flags, int):
        raise QuailSyntaxError("Regex flags must be an int")
    if flags & ~ALLOWED_REGEX_FLAGS:
        raise QuailSyntaxError("Regex flags are limited to re.A, re.I, re.M, re.S, and re.U")
    if flags & int(python_re.A) and flags & int(python_re.U):
        raise QuailSyntaxError("Regex flags re.A and re.U cannot be combined")


def require_regex_text(value: str, label: str) -> bytes:
    if not isinstance(value, str):
        raise QuailSyntaxError(f"{label} must be a string")
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_REGEX_PATTERN_BYTES:
        raise QuailSyntaxError(f"{label} cannot exceed {MAX_REGEX_PATTERN_BYTES} UTF-8 bytes")
    return encoded
