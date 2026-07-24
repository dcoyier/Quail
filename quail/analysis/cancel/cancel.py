"""Cancel an in-flight host Turso call when wall/memory fires."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

from quail.analysis.errors import QuailRuntimeError
from quail.analysis.limits import ExecLimits, wall_timeout_error


def raise_if_cancelled(
    cancel_event: threading.Event | None,
    *,
    limits: ExecLimits | None = None,
    wall_exceeded: threading.Event | None = None,
) -> None:
    """Raise when the host cancel event is set (prefer wall diagnostic)."""

    if cancel_event is None or not cancel_event.is_set():
        return
    if wall_exceeded is not None and wall_exceeded.is_set() and limits is not None:
        raise wall_timeout_error(
            limits.wall_seconds,
            already_extended=limits.already_extended,
        )
    raise QuailRuntimeError(
        "quail_exec was cancelled",
        repair_hint="Retry the whole exec.",
    )


@contextmanager
def interrupt_connections_on_cancel(
    connections: Iterable[Any],
    cancel_event: threading.Event | None,
) -> Iterator[None]:
    """Best-effort ``connection.interrupt()`` when ``cancel_event`` is set.

    Does not set ``cancel_event`` on normal exit (avoids poisoning a shared
    caller-owned Event after a successful exec).
    """

    if cancel_event is None:
        yield
        return

    targets = [connection for connection in connections if connection is not None]
    if not targets:
        yield
        return

    completed = threading.Event()

    def watch() -> None:
        while not completed.is_set():
            if cancel_event.wait(0.01):
                if completed.is_set():
                    return
                for connection in targets:
                    with suppress(Exception):
                        interrupt = getattr(connection, "interrupt", None)
                        if callable(interrupt):
                            interrupt()
                return

    watcher = threading.Thread(
        target=watch,
        name="quail-connection-cancel",
        daemon=True,
    )
    watcher.start()
    try:
        yield
    finally:
        completed.set()
        watcher.join(timeout=1)
