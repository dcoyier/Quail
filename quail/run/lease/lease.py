"""Exclusive flock lease so process, serve, and apply_config do not share live state."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from quail.analysis.errors import QuailRuntimeError
from quail.config.models import QuailConfig

_LEASE_REPAIR = (
    "Stop the running Quail server (or other quail process holding this "
    "deployment), then retry. process and run must not share the same "
    "core/search databases concurrently."
)


def _lock_path_for(database_path: Path) -> Path:
    """Lock file beside a Turso/SQLite path (same directory, ``.quail.lock`` suffix)."""

    resolved = Path(database_path).expanduser().resolve()
    return resolved.with_name(resolved.name + ".quail.lock")


def _read_holder_pid(lock_path: Path) -> int | None:
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text.isdigit():
        return int(text)
    return None


def _write_holder_pid(handle: IO[str]) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()


@contextmanager
def acquire_deployment_lease(config: QuailConfig) -> Iterator[tuple[Path, ...]]:
    """Acquire exclusive non-blocking flocks for core (+ search when set).

    Lock files are taken in sorted path order. POSIX local filesystems only;
    NFS lock semantics vary and Windows is unsupported.
    """

    paths = [Path(config.database).expanduser().resolve()]
    if config.search_database is not None:
        paths.append(Path(config.search_database).expanduser().resolve())
    lock_paths = tuple(sorted({_lock_path_for(path) for path in paths}))
    handles: list[IO[str]] = []
    try:
        for lock_path in lock_paths:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                handle.close()
                holder = _read_holder_pid(lock_path)
                if holder is not None:
                    raise QuailRuntimeError(
                        "Another Quail process "
                        f"(PID {holder}) holds the deployment lease "
                        f"({lock_path.name}).",
                        repair_hint=(
                            f"Stop PID {holder} (the other quail process or run), "
                            "then retry. process and run must not share the same "
                            "core/search databases concurrently."
                        ),
                    ) from error
                raise QuailRuntimeError(
                    "Another Quail process holds the deployment lease "
                    f"({lock_path.name}).",
                    repair_hint=_LEASE_REPAIR,
                ) from error
            _write_holder_pid(handle)
            handles.append(handle)
        yield lock_paths
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
