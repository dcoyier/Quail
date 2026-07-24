"""Provider errors."""

from __future__ import annotations


class ProviderError(RuntimeError):
    """Embedding provider configuration or HTTP failure."""

    def __init__(self, message: str, *, repair_hint: str | None = None) -> None:
        super().__init__(message)
        self.repair_hint = repair_hint
