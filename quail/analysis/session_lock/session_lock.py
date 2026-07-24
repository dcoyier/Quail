"""In-process lock: one quail_exec at a time per session_id."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from quail.analysis.errors import QuailSessionBusyError

_held: set[str] = set()
_lock = threading.Lock()

_BUSY_REPAIR = (
    "Wait for the in-flight quail_exec on this session_id to finish, "
    "then retry. Do not overlap execs on the same session_id."
)


@contextmanager
def acquire_session_lock(session_id: str) -> Iterator[None]:
    """Take the session lock or raise QuailSessionBusyError without blocking."""

    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    key = session_id.strip()
    with _lock:
        if key in _held:
            raise QuailSessionBusyError(
                "Another quail_exec is already running for this session_id.",
                repair_hint=_BUSY_REPAIR,
            )
        _held.add(key)
    try:
        yield
    finally:
        with _lock:
            _held.discard(key)


def reset_session_locks_for_tests() -> None:
    """Clear held session locks (test helper)."""

    with _lock:
        _held.clear()
