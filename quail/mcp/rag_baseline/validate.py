"""Input validation for opaque search tool parameters."""

from __future__ import annotations

from quail.analysis.errors import QuailSyntaxError

_MAX_QUERY_CHARS = 2000
_DEFAULT_TOP_K = 8
_MAX_TOP_K = 20


def normalize_query(query: object) -> str:
    """Require a non-empty query string within the character ceiling."""

    if not isinstance(query, str):
        raise QuailSyntaxError("query must be a string")
    stripped = query.strip()
    if not stripped:
        raise QuailSyntaxError("query must be a non-empty string")
    if len(stripped) > _MAX_QUERY_CHARS:
        raise QuailSyntaxError(
            f"query exceeds {_MAX_QUERY_CHARS} characters (got {len(stripped)})"
        )
    return stripped


def normalize_top_k(top_k: object = _DEFAULT_TOP_K) -> int:
    """Require an integer top_k in 1..20 (reject bool)."""

    if top_k is None:
        return _DEFAULT_TOP_K
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise QuailSyntaxError("top_k must be an integer")
    if top_k < 1 or top_k > _MAX_TOP_K:
        raise QuailSyntaxError(f"top_k must be between 1 and {_MAX_TOP_K} (got {top_k})")
    return top_k
