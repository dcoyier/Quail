"""Search package errors."""

from __future__ import annotations


class SearchError(RuntimeError):
    """Search database or Semantic scoring failure."""

    def __init__(self, message: str, *, repair_hint: str | None = None) -> None:
        super().__init__(message)
        self.repair_hint = repair_hint
