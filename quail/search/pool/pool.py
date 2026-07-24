"""Bounded SearchDb connection pool for concurrent quail_exec."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from quail.analysis.errors import QuailServerBusyError
from quail.search.db import SearchDb, open_search_db

_BUSY_REPAIR = "Retry after another quail_exec call finishes."


@dataclass(slots=True)
class SearchDbPool:
    """Checkout up to ``max_size`` SearchDb handles for one search file."""

    path: Path
    max_size: int
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _idle: list[SearchDb] = field(default_factory=list, init=False, repr=False)
    _checked_out: int = field(default=0, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def checkout(self) -> SearchDb:
        """Take one SearchDb or open a new one up to ``max_size``."""

        with self._lock:
            if self._closed:
                raise RuntimeError("SearchDbPool is closed")
            if self._idle:
                self._checked_out += 1
                return self._idle.pop()
            if self._checked_out >= self.max_size:
                raise QuailServerBusyError(
                    "Quail is at its concurrent execution limit.",
                    repair_hint=_BUSY_REPAIR,
                )
            self._checked_out += 1
        try:
            return open_search_db(self.path)
        except Exception:
            with self._lock:
                self._checked_out -= 1
            raise

    def release(self, search: SearchDb) -> None:
        """Return a checked-out SearchDb to the idle pool."""

        with self._lock:
            if self._closed:
                search.close()
                return
            if self._checked_out <= 0:
                search.close()
                return
            self._checked_out -= 1
            self._idle.append(search)

    def close(self) -> None:
        """Close idle connections; checked-out handles close on release."""

        with self._lock:
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
        for search in idle:
            search.close()

    @contextmanager
    def connection(self) -> Iterator[SearchDb]:
        """Checkout for a ``with`` block and always release."""

        search = self.checkout()
        try:
            yield search
        finally:
            self.release(search)


def open_search_pool(path: str | Path, *, max_size: int) -> SearchDbPool:
    """Build a pool for ``path`` sized to concurrent exec capacity."""

    if (
        isinstance(max_size, bool)
        or not isinstance(max_size, int)
        or max_size < 1
    ):
        raise ValueError("max_size must be a positive integer")
    return SearchDbPool(path=Path(path).expanduser().resolve(), max_size=max_size)
