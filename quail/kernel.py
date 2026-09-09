"""A session's execution owner: child lifetime, limits, and durable completion.

Only the calling thread touches the index and run log. A small monitor samples
process resources independently, including while Python or provider I/O is busy.
The local adapter transports requests; this module alone decides cell outcomes.
"""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from quail import history, wire
from quail.contracts import (
    CellReply,
    ErrorInfo,
    Execution,
    JSONObject,
    Limits,
    QuailError,
    json_object,
)
from quail.index import Applied, Index
from quail.project import Project, SessionMetadata

type Spawn = Callable[[list[str], dict[str, str], tuple[int, ...]], subprocess.Popen[bytes]]


@dataclass(frozen=True)
class CellIdentity:
    run: str
    cell: int

    def to_record(self) -> JSONObject:
        return {"run": self.run, "cell": self.cell}


@dataclass(frozen=True)
class Runtime:
    state: Literal["idle", "busy", "stopped", "unavailable"]
    run: str | None = None
    cell: int | None = None
    last_completed: CellIdentity | None = None
    limits: Limits | None = None
    reason: str | None = None

    def to_record(self) -> JSONObject:
        return {
            "state": self.state,
            "run": self.run,
            "cell": self.cell,
            "last_completed": self.last_completed.to_record() if self.last_completed else None,
            "limits": self.limits.to_record() if self.limits else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Usage:
    rss: int
    cpu: float


def _usage(pid: int) -> Usage:
    """Read the supported OS counters; unavailable enforcement is an error."""
    if sys.platform == "linux":
        root = Path("/proc") / str(pid)
        pages = int((root / "statm").read_text().split()[1])
        # The command name may contain spaces or parentheses; fields start after it.
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        ticks = int(fields[11]) + int(fields[12])
        return Usage(pages * os.sysconf("SC_PAGE_SIZE"), ticks / os.sysconf("SC_CLK_TCK"))
    if sys.platform == "darwin":
        row = subprocess.run(
            ["ps", "-o", "rss=,time=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=True,
            timeout=1,
        ).stdout.split()
        clock = row[1]
        days, separator, clock_part = clock.partition("-")
        seconds = 0.0
        for part in (clock_part if separator else clock).split(":"):
            seconds = seconds * 60 + float(part)
        return Usage(int(row[0]) * 1024, seconds + (int(days) * 86400 if separator else 0))
    raise QuailError("Kernel resource monitoring requires Linux or macOS")


class _Monitor:
    def __init__(self, process: subprocess.Popen[bytes], limits: Limits) -> None:
        self.process = process
        self.maximum = limits.memory_mb * 1024 * 1024
        self.latest: Usage | None = None
        self.failure: str | None = None
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="quail-resources", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        misses = 0
        while not self.stopped.is_set() and self.process.poll() is None:
            try:
                self.latest = _usage(self.process.pid)
                misses = 0
                if self.latest.rss > self.maximum:
                    self.failure = "Kernel RSS limit exceeded"
            except (
                OSError,
                ValueError,
                IndexError,
                subprocess.SubprocessError,
                QuailError,
            ) as error:
                misses += 1
                if misses >= 5:
                    self.failure = f"Cannot monitor kernel resources: {error}"
            if self.failure:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                self.ready.set()
                return
            if self.latest is not None:
                self.ready.set()
            self.stopped.wait(0.1)

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise QuailError("Kernel resource monitor did not stop")


def _spawn(
    argv: list[str], environment: dict[str, str], descriptors: tuple[int, ...]
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        env=environment,
        pass_fds=descriptors,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class _Child:
    """One confined process and its private pipes/scratch, with bounded I/O."""

    def __init__(self, project: Project, index: Index, session: str, spawn: Spawn) -> None:
        self.resources = ExitStack()
        self.process: subprocess.Popen[bytes] | None = None
        self.monitor: _Monitor | None = None
        self.maximum = max(1024 * 1024, project.limits.memory_mb * 1024 * 1024 // 4)
        self.decoder = wire.Decoder(self.maximum)
        self.pending: deque[JSONObject] = deque()
        root = project.path(".quail", "children")
        root.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="kernel-", dir=root))
        self.resources.callback(shutil.rmtree, scratch)
        try:
            child_in, self.outgoing = os.pipe()
            self.incoming, child_out = os.pipe()
            self.resources.callback(os.close, self.incoming)
            self.resources.callback(os.close, self.outgoing)
            environment = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "LANG", "TZ"} or key.startswith("LC_")
            }
            environment.update(
                QUAIL_CONTROL_IN=str(child_in),
                QUAIL_CONTROL_OUT=str(child_out),
                SQLITE_TMPDIR=str(scratch),
                PYTHONDONTWRITEBYTECODE="1",
            )
            try:
                self.process = spawn(
                    [sys.executable, "-m", "quail.prelude"], environment, (child_in, child_out)
                )
            finally:
                os.close(child_in)
                os.close(child_out)
            self.monitor = _Monitor(self.process, project.limits)
            os.set_blocking(self.incoming, False)
            os.set_blocking(self.outgoing, False)
            self.send(
                wire.encode(
                    {
                        "type": "bootstrap",
                        "index": str(index.path),
                        "session": session,
                        "source": index.source.to_record(),
                        "limits": project.limits.to_record(),
                    }
                ),
                self.check,
            )
            ready = self.receive(self.check)
            while not self.monitor.ready.wait(0.1):
                self.check()
            self.check()
            if ready.keys() != {"type", "confinement"} or ready["type"] != "ready":
                raise QuailError("Child did not report readiness")
            confinement = ready["confinement"]
            if confinement not in {"audit", "audit+netns"}:
                raise QuailError("Child reported invalid confinement")
            assert isinstance(confinement, str)
            self.confinement = confinement
        except BaseException:
            self.close()
            raise

    def check(self) -> None:
        if self.monitor is not None and self.monitor.failure:
            raise QuailError(self.monitor.failure)
        process = self.process
        if process is None or process.poll() is not None:
            raise QuailError("Kernel process exited before completing the operation")

    def send(self, payload: bytes, tick: Callable[[], None]) -> None:
        remaining = memoryview(payload)
        with selectors.DefaultSelector() as selector:
            selector.register(self.outgoing, selectors.EVENT_WRITE)
            while remaining:
                tick()
                if selector.select(0.05):
                    try:
                        count = os.write(self.outgoing, remaining[:65536])
                    except BlockingIOError:
                        continue
                    remaining = remaining[count:]

    def receive(self, tick: Callable[[], None]) -> JSONObject:
        with selectors.DefaultSelector() as selector:
            selector.register(self.incoming, selectors.EVENT_READ)
            while not self.pending:
                # Drain a complete reply before testing liveness: a child may have
                # exited after sending a result the host can still durably finish.
                if selector.select(0.05):
                    try:
                        data = os.read(self.incoming, 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        self.check()
                        raise EOFError("Kernel control channel closed")
                    self.pending.extend(self.decoder.feed(data))
                    if self.pending:
                        break
                tick()
        record = self.pending.popleft()
        if record.get("type") == "fatal":
            error = ErrorInfo.from_record(json_object(record.get("error")))
            raise QuailError(
                f"Kernel bootstrap/control failure: {error.type}: {error.message}", error.hint
            )
        return record

    def close(self) -> None:
        # Closing the host pipe stops even an executing child. Reap it before
        # deleting SQLite scratch or allowing another owner to take the locks.
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
            self.process = None
        if self.monitor is not None:
            self.monitor.close()
            self.monitor = None
        self.resources.close()


class Kernel:
    """A ready session, owned synchronously by the thread that opened it."""

    def __init__(
        self,
        project: Project,
        session: SessionMetadata,
        index: Index,
        resources: ExitStack,
        applied: Applied,
        snapshot: history.Snapshot,
        *,
        spawn: Spawn | None = None,
    ) -> None:
        self.project, self.session, self.index = project, session, index
        self._resources = resources
        self._owner = threading.get_ident()
        self._spawn = spawn or _spawn
        self._applied = applied
        self._hashes = snapshot.hashes
        self._closed = False
        self._child: _Child | None = None
        self._log: history.RunLog | None = None
        self._runtime = Runtime("idle", limits=project.limits)
        self._warnings = [
            "Started a fresh Python kernel; committed tags were restored.",
            *applied.summary.warnings,
        ]
        previous = applied.summary.last_source_version or session.source_version
        if previous != index.source.version:
            self._warnings.append(
                "Source version changed; review annotations against the current text"
            )
        try:
            self._start_run()
        except BaseException:
            self.close()
            raise

    @property
    def runtime(self) -> Runtime:
        snapshot = self._runtime
        child = self._child
        if child is not None:
            try:
                child.check()
            except QuailError as error:
                return replace(snapshot, state="unavailable", reason=str(error))
        return snapshot

    def _owned(self) -> None:
        if threading.get_ident() != self._owner:
            raise QuailError("Kernel operations belong to the thread that opened the session")
        if self._closed:
            raise QuailError("Kernel is closed")
        if self._runtime.state == "busy":
            raise QuailError("Kernel is busy", "Wait for the accepted cell to complete")

    def _start_run(self) -> None:
        child = _Child(self.project, self.index, self.session.name, self._spawn)
        log = None
        try:
            source = self.index.source
            config = self.project.dataset(self.session.dataset).embedding
            header = history.RunHeader(
                history.RunLog.new_id(),
                history.now(),
                os.environ.get("QUAIL_ACTOR") or os.uname().nodename,
                version("quail"),
                self.session.dataset,
                source.hash,
                source.version,
                source.id_column,
                config.descriptor() if config else None,
                child.confinement,
            )
            log = history.RunLog(self.project.session_path(self.session.name) / "log", header)
            self._hashes[log.path.name] = log.digest
            summary = replace(self._applied.summary, runs=self._applied.summary.runs + 1)
            applied = Applied(
                history.history_digest(self._hashes), source.version, self._applied.orphans, summary
            )
            self.index.complete(self.session.name, {}, applied)
        except BaseException:
            if log is not None:
                log.close()
            child.close()
            raise
        self._child, self._log, self._applied = child, log, applied
        self._runtime = replace(self._runtime, state="idle", run=header.run, cell=None, reason=None)

    def _discard_run(self) -> None:
        child, self._child = self._child, None
        log, self._log = self._log, None
        try:
            if child is not None:
                child.close()
        finally:
            if log is not None:
                log.close()

    def exec(
        self, code: str, *, on_accept: Callable[[CellIdentity], None] | None = None
    ) -> Execution:
        self._owned()
        restarted = False
        assert self._child is not None
        try:
            self._child.check()
        except QuailError:
            self._discard_run()
            self._start_run()
            restarted = True
        child, log = self._child, self._log
        assert (
            child is not None
            and child.process is not None
            and child.monitor is not None
            and log is not None
        )
        number = log.next_cell
        # Invalid/unencodable input has not been accepted and creates no cell.
        payload = wire.encode({"type": "run", "n": number, "code": code}, child.maximum)
        identity = CellIdentity(log.header.run, number)
        self._runtime = replace(self._runtime, state="busy", cell=number)
        started, wall_start = history.now(), time.monotonic()
        try:
            baseline: float | None = _usage(child.process.pid).cpu
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            baseline = None  # The monitor tolerates a transient missed sample.
        expired: str | None = None
        interrupted: float | None = None

        def tick() -> None:
            nonlocal baseline, expired, interrupted
            child.check()
            assert child.process is not None and child.monitor is not None
            current = time.monotonic()
            usage = child.monitor.latest
            if baseline is None and usage is not None:
                baseline = usage.cpu
            if expired is None:
                if current - wall_start > self.project.limits.wall_seconds:
                    expired = "Wall limit exceeded"
                elif (
                    usage is not None
                    and baseline is not None
                    and usage.cpu - baseline > self.project.limits.cpu_seconds
                ):
                    expired = "CPU limit exceeded"
                if expired:
                    interrupted = current
                    child.process.send_signal(
                        signal.SIGINT if expired.startswith("Wall") else signal.SIGPROF
                    )
            if interrupted is not None and current - interrupted >= 5:
                child.process.kill()
                raise QuailError(expired or "Kernel did not stop after interruption")

        replacement_needed = False
        try:
            if on_accept is not None:
                try:
                    on_accept(identity)
                except OSError:
                    pass  # Loss of the observer cannot cancel accepted execution.
            try:
                child.send(payload, tick)
                reply = CellReply.from_record(child.receive(tick), number)
                self.index.validate_delta(reply.tags)
                if time.monotonic() - wall_start > self.project.limits.wall_seconds:
                    expired = expired or "Wall limit exceeded"
                if expired:
                    replacement_needed = reply.error is None
                    reply = replace(reply, error=ErrorInfo("QuailError", expired), tags={})
            except (OSError, EOFError, QuailError) as error:
                reason = expired or str(error)
                reply = CellReply(number, "", ErrorInfo("QuailError", reason), False, {})
                replacement_needed = True
            cell = history.CellRecord(
                number,
                self._applied.summary.max_order + 1,
                started,
                history.now(),
                code,
                reply.output,
                reply.error,
                reply.truncated,
                reply.tags,
            )
            self._complete(log, cell)
            self._runtime = replace(self._runtime, state="idle", cell=None, last_completed=identity)
            try:
                child.check()
            except QuailError:
                replacement_needed = True
            if replacement_needed:
                try:
                    self._discard_run()
                    self._start_run()
                except BaseException as error:
                    raise QuailError(
                        f"Cell {identity.run}/{number} is committed in {log.path}, "
                        f"but kernel replacement failed: {error}",
                        "Read the recorded result; do not resubmit the cell automatically",
                    ) from error
                restarted = True
            warnings, self._warnings = tuple(self._warnings), []
            return Execution(
                self.session.name, identity.run, reply, self.project.limits, warnings, restarted
            )
        except BaseException:
            self.close()
            raise

    def _complete(self, log: history.RunLog, cell: history.CellRecord) -> None:
        try:
            log.append(cell)
        except BaseException as error:
            raise QuailError(
                f"Persistence outcome is uncertain for {log.header.run}/{cell.n} "
                f"in {log.path}: {error}",
                "Reopen to recover records on disk; do not resubmit the cell automatically",
            ) from error
        self._hashes[log.path.name] = log.digest
        summary = replace(self._applied.summary)
        summary.completed(log.header, cell)
        applied = Applied(
            history.history_digest(self._hashes),
            self.index.source.version,
            self._applied.orphans,
            summary,
        )
        try:
            try:
                self.index.complete(self.session.name, cell.tags, applied)
            except sqlite3.Error:
                # The synced record is truth. Recover only the observed history,
                # never newly pulled files and never by executing Python again.
                directory = log.path.parent
                observed = history.Snapshot(
                    tuple(
                        history.LogFile(directory / name, digest)
                        for name, digest in sorted(self._hashes.items())
                    )
                )
                applied = self.index.synchronize(self.session, observed)
        except BaseException as error:
            raise QuailError(
                f"Cell {log.header.run}/{cell.n} is committed in {log.path}, "
                f"but cache recovery failed: {error}",
                "Keep the log and reopen; do not execute the code again",
            ) from error
        self._applied = applied

    def reset(self) -> Runtime:
        self._owned()
        try:
            self._discard_run()
            self._start_run()
            return self.runtime
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if threading.get_ident() != self._owner:
            raise QuailError("Close the Kernel on its execution owner's thread")
        if self._closed:
            return
        self._closed = True
        try:
            self._discard_run()
        finally:
            self._resources.close()
            self._runtime = replace(self._runtime, state="stopped", run=None, cell=None)

    def __enter__(self) -> Kernel:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()
