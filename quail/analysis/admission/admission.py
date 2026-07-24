"""Process-wide quail_exec admission (fail-fast when full)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from quail.analysis.errors import QuailServerBusyError

_DEFAULT_SLOTS = 2
_slots: threading.BoundedSemaphore | None = None
_configured_n: int | None = None
_lock = threading.Lock()

_BUSY_REPAIR = "Retry after another quail_exec call finishes."


def configure_execution_slots(max_concurrent_executions: int) -> None:
    """Install the process-wide exec slot pool from quail.toml hosting."""

    if (
        isinstance(max_concurrent_executions, bool)
        or not isinstance(max_concurrent_executions, int)
        or max_concurrent_executions < 1
    ):
        raise ValueError("max_concurrent_executions must be a positive integer")
    global _slots, _configured_n
    with _lock:
        _slots = threading.BoundedSemaphore(max_concurrent_executions)
        _configured_n = max_concurrent_executions


def reset_execution_slots_for_tests() -> None:
    """Restore default slots (test helper)."""

    configure_execution_slots(_DEFAULT_SLOTS)


def configured_execution_slots() -> int:
    """Return the configured ceiling (default until configure is called)."""

    with _lock:
        return _DEFAULT_SLOTS if _configured_n is None else _configured_n


@contextmanager
def acquire_execution_slot() -> Iterator[None]:
    """Take one exec slot or raise QuailServerBusyError without blocking."""

    global _slots, _configured_n
    with _lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(_DEFAULT_SLOTS)
            _configured_n = _DEFAULT_SLOTS
        pool = _slots
    if not pool.acquire(blocking=False):
        raise QuailServerBusyError(
            "Quail is at its concurrent execution limit.",
            repair_hint=_BUSY_REPAIR,
        )
    try:
        yield
    finally:
        pool.release()
