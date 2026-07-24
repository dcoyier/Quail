"""Public exports for ``quail.analysis.cancel``."""

from .cancel import interrupt_connections_on_cancel, raise_if_cancelled

__all__ = [
    "interrupt_connections_on_cancel",
    "raise_if_cancelled",
]
