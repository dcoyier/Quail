"""Text segment extraction and Lexical entry-target rewrite helpers."""

from __future__ import annotations

import re
from typing import Any

from quail.analysis.errors import QuailRuntimeError

_LEXICAL_DOCUMENT_TERM = re.compile(r"\w+", flags=re.UNICODE)


def text_segments(value: Any, *, operation_kind: str) -> tuple[str, ...]:
    """Give back non-empty text segments from a field or literal value."""

    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(item for item in value if item)
    raise QuailRuntimeError(f"{operation_kind} requires text, list[text], or None")


def lexical_document_query(text: str) -> str:
    """Represent corpus text as quoted FTS terms, never as executable syntax."""

    return " ".join(f'"{term}"' for term in _LEXICAL_DOCUMENT_TERM.findall(text))
