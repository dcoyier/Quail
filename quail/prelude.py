"""The single child entry point: bootstrap, persistent cells, and confinement.

Keep imports above bootstrap lightweight. Linux namespaces and the native-thread
budget must be established before the language imports numerical libraries.
The child owns no manifest, provider credentials, durable logs, or host locks.
"""

from __future__ import annotations
import __future__

import ast
import builtins
import collections
import contextlib
import io
import itertools
import json
import linecache
import math
import os
import queue
import re
import signal
import sqlite3
import statistics
import sys
import sysconfig
import threading
import traceback
import types
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, NoReturn

from quail import wire
from quail.contracts import (
    CellReply,
    EmbeddingReply,
    EmbeddingRequest,
    ErrorInfo,
    FieldInfo,
    JSONObject,
    Limits,
    QuailError,
    Source,
    checked_hash,
    json_object,
)

if TYPE_CHECKING:
    from quail.language.evaluator import Evaluator


class BoundedOutput(io.TextIOBase):
    """Retain a UTF-8 prefix while counting, rather than retaining, excess output."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.kept = bytearray()
        self.omitted = 0

    encoding = "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        # Encoding in chunks avoids doubling an arbitrarily large print argument.
        for start in range(0, len(text), 4096):
            chunk = text[start : start + 4096].encode("utf-8", errors="backslashreplace")
            available = 0 if self.omitted else self.limit - len(self.kept)
            prefix = chunk[:available].decode("utf-8", errors="ignore").encode("utf-8")
            self.kept.extend(prefix)
            self.omitted += len(chunk) - len(prefix)
        return len(text)

    def finish(self) -> str:
        text = self.kept.decode("utf-8")
        if self.omitted:
            text += f"\n[output truncated; {self.omitted} UTF-8 bytes omitted]\n"
        return text


def public_namespace(evaluator: Evaluator) -> dict[str, object]:
    from quail.language.evaluator import Entry
    from quail.language.expressions import Expression, Predicate, constructors

    field, random = constructors(evaluator.state)
    operations: dict[str, object] = {
        "count": evaluator.count,
        "retrieve": evaluator.retrieve,
        "values": evaluator.values,
        "tag": evaluator.tag,
        "fields": evaluator.fields,
        "Field": field,
        "Random": random,
        "Expression": Expression,
        "Predicate": Predicate,
        "Entry": Entry,
        "FieldInfo": FieldInfo,
        "QuailError": QuailError,
    }
    return operations | {
        "quail": types.SimpleNamespace(**operations),
        "re": re,
        "math": math,
        "statistics": statistics,
        "json": json,
        "itertools": itertools,
        "collections": collections,
        "Counter": collections.Counter,
    }


class Runner:
    """One module namespace and one compiler state for the lifetime of a child."""

    def __init__(self, evaluator: Evaluator, *, timers: bool = True) -> None:
        self.evaluator = evaluator
        self.limits = evaluator.state.limits
        self.module = types.ModuleType("_quail_session_" + uuid.uuid4().hex)
        self.module.__dict__.update(public_namespace(evaluator))
        self.module.__dict__["__builtins__"] = builtins
        sys.modules[self.module.__name__] = self.module
        self.flags = 0
        self._future_mask = 0
        for name in __future__.all_feature_names:
            self._future_mask |= getattr(__future__, name).compiler_flag
        self._sources: collections.OrderedDict[str, int] = collections.OrderedDict()
        self._source_bytes = 0
        self._source_budget = min(8 * 1024 * 1024, self.limits.memory_mb * 1024 * 1024 // 32)
        self.expired: str | None = None
        self.exchanging_embeddings = False
        self.timers = timers
        if timers:
            signal.signal(signal.SIGPROF, self._cpu_limit)
            signal.signal(signal.SIGINT, self._wall_limit)
            evaluator.state.connection.set_progress_handler(self._interrupted_sql, 1000)

    def _cpu_limit(self, signum: int, frame: types.FrameType | None) -> None:
        self.expired = "CPU limit exceeded"
        if not self.exchanging_embeddings:
            raise QuailError(self.expired)

    def _wall_limit(self, signum: int, frame: types.FrameType | None) -> None:
        self.expired = "Wall limit exceeded"
        if not self.exchanging_embeddings:
            raise QuailError(self.expired)

    def _interrupted_sql(self) -> int:
        return int(self.expired is not None)

    def _remember_source(self, filename: str, code: str) -> None:
        size = len(code.encode("utf-8"))
        if size > self._source_budget:
            return
        linecache.cache[filename] = (size, None, code.splitlines(keepends=True), filename)
        self._sources[filename] = size
        self._source_bytes += size
        while self._source_bytes > self._source_budget:
            name, removed = self._sources.popitem(last=False)
            linecache.cache.pop(name, None)
            self._source_bytes -= removed

    def _execute(self, code: str, filename: str, output: BoundedOutput) -> None:
        self._remember_source(filename, code)
        module = compile(
            code, filename, "exec", flags=self.flags | ast.PyCF_ONLY_AST, dont_inherit=True
        )
        assert isinstance(module, ast.Module)
        last = module.body.pop() if module.body and isinstance(module.body[-1], ast.Expr) else None
        statements = compile(module, filename, "exec", flags=self.flags, dont_inherit=True)
        self.flags |= statements.co_flags & self._future_mask
        display = (
            compile(
                ast.Expression(last.value), filename, "eval", flags=self.flags, dont_inherit=True
            )
            if isinstance(last, ast.Expr)
            else None
        )
        namespace = self.module.__dict__
        exec(statements, namespace, namespace)
        if display is not None:
            value = eval(display, namespace, namespace)
            if value is not None:
                output.write(repr(value))
                output.write("\n")

    def run(self, n: int, code: str) -> CellReply:
        output = BoundedOutput(self.limits.output_kib * 1024)
        error_info = None
        tags = {}
        self.expired = None
        self.evaluator.begin()
        if self.timers:
            signal.setitimer(signal.ITIMER_PROF, self.limits.cpu_seconds)
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                try:
                    self._execute(code, f"<{self.module.__name__}:cell-{n}>", output)
                    if self.expired:
                        raise QuailError(self.expired)
                    tags = self.evaluator.commit()
                except BaseException as caught:
                    self.evaluator.rollback()
                    failure: BaseException = QuailError(self.expired) if self.expired else caught
                    error_info = _error_info(failure, output.limit)
                    _format_exception(failure, output)
        finally:
            if self.timers:
                signal.setitimer(signal.ITIMER_PROF, 0)
        return CellReply(n, output.finish(), error_info, bool(output.omitted), tags)

    def close(self) -> None:
        self.evaluator.close()
        sys.modules.pop(self.module.__name__, None)
        for name in self._sources:
            linecache.cache.pop(name, None)


def _error_info(error: BaseException, limit: int) -> ErrorInfo:
    try:
        original = ErrorInfo.from_exception(error)
    except BaseException:
        original = ErrorInfo(type(error).__name__, "Exception message could not be formatted")
    message = BoundedOutput(limit)
    message.write(original.message)
    hint = BoundedOutput(limit)
    if original.hint is not None:
        hint.write(original.hint)
    return ErrorInfo(original.type, message.finish(), hint.finish() if original.hint else None)


def _format_exception(error: BaseException, output: BoundedOutput) -> None:
    # Drop runtime frames before traceback formatting tries to read their files;
    # those implementation paths deliberately are not granted to analysis code.
    formatted = traceback.TracebackException.from_exception(
        error, capture_locals=False, lookup_lines=False
    )
    root = str(Path(__file__).resolve().parent) + os.sep

    def trim(item: traceback.TracebackException) -> None:
        item.stack = traceback.StackSummary.from_list(
            [frame for frame in item.stack if not frame.filename.startswith(root)]
        )
        for child in (item.__cause__, item.__context__, *(item.exceptions or [])):
            if child is not None:
                trim(child)

    trim(formatted)
    for part in formatted.format():
        output.write(part)
    if isinstance(error, QuailError) and error.hint:
        output.write("Hint: " + error.hint + "\n")


def _deny_file(*args: object, **kwargs: object) -> NoReturn:
    raise QuailError(
        "File access is unavailable inside a cell",
        "Prepare files outside Quail and use the analysis verbs inside it",
    )


def install_confinement(connection: sqlite3.Connection) -> None:
    """Subtract accidental I/O capabilities after all runtime resources are open."""
    import numpy
    import re2

    standard = {Path(sysconfig.get_path(name)).resolve() for name in ("stdlib", "platstdlib")}
    packages = set()
    for module in (numpy, re2):
        filename = module.__file__
        if not isinstance(filename, str):
            raise QuailError("Cannot resolve a numerical package for confinement")
        packages.add(Path(filename).resolve().parent)
    writes = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    denied = {
        "os.remove",
        "os.rename",
        "os.mkdir",
        "os.rmdir",
        "os.link",
        "os.symlink",
        "os.truncate",
        "os.chmod",
        "os.chown",
        "os.utime",
        "os.setxattr",
        "os.removexattr",
        "sqlite3.connect",
        "sqlite3.enable_load_extension",
        "sqlite3.load_extension",
        "subprocess.Popen",
        "os.system",
        "os.fork",
        "os.forkpty",
        "os.exec",
        "os.posix_spawn",
        "sys.addaudithook",
        "sys.setprofile",
        "sys.settrace",
    }

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event in denied or event.startswith(("socket.", "ctypes.", "shutil.")):
            raise QuailError(f"Capability unavailable inside a cell: {event}")
        if event == "open":
            path, _, flags = args
            if (
                not isinstance(path, (str, bytes, os.PathLike))
                or not isinstance(flags, int)
                or flags & writes
            ):
                _deny_file()
            resolved = Path(os.fsdecode(path)).resolve()
            allowed_package = any(resolved.is_relative_to(root) for root in packages)
            allowed_standard = any(resolved.is_relative_to(root) for root in standard)
            if not allowed_package and (
                not allowed_standard
                or {"site-packages", "dist-packages"}.intersection(resolved.parts)
            ):
                _deny_file()

    write_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_CREATE_VTABLE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_DROP_VTABLE,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_REINDEX,
        sqlite3.SQLITE_ANALYZE,
    }

    def authorize(
        action: int,
        first: str | None,
        second: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and second == "load_extension":
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and second is not None:
            return sqlite3.SQLITE_DENY
        if action in write_actions and database != "temp":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    sys.dont_write_bytecode = True
    for module, names in (
        (builtins, ("open",)),
        (io, ("open",)),
        (os, ("open", "fdopen", "mkfifo", "mknod")),
    ):
        for name in names:
            if hasattr(module, name):
                setattr(module, name, _deny_file)
    sys.addaudithook(audit)


def _network_namespace() -> str:
    if sys.platform == "linux" and hasattr(os, "unshare"):
        try:
            os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)
            return "audit+netns"
        except OSError:
            pass
    return "audit"


def _watch_control(stream: BinaryIO, inbox: queue.Queue[JSONObject], maximum: int) -> None:
    try:
        while True:
            inbox.put_nowait(wire.receive(stream, maximum))
    except BaseException:
        # This is the host-owned pipe, not a CLI connection. Even an executing
        # infinite loop must end when its host dies.
        os._exit(1)


def main() -> int:
    incoming = os.fdopen(int(os.environ.pop("QUAIL_CONTROL_IN")), "rb", buffering=0)
    outgoing = os.fdopen(int(os.environ.pop("QUAIL_CONTROL_OUT")), "wb", buffering=0)
    runner: Runner | None = None
    maximum = wire.DEFAULT_MAX_FRAME
    try:
        configuration = wire.receive(incoming)
        if configuration.get("type") != "bootstrap":
            raise QuailError("Expected a bootstrap record")
        source = Source.from_record(json_object(configuration.get("source")))
        limits = Limits.from_record(json_object(configuration.get("limits")))
        index, session = configuration.get("index"), configuration.get("session")
        configured_identity = configuration.get("embedding_id")
        identity = (
            checked_hash(configured_identity, "embedding identity")
            if configured_identity is not None
            else None
        )
        if not isinstance(index, str) or not isinstance(session, str):
            raise QuailError("Invalid child scope")
        maximum = max(1024 * 1024, limits.memory_mb * 1024 * 1024 // 4)
        confinement = _network_namespace()
        for setting in (
            "OPENBLAS_NUM_THREADS",
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        ):
            os.environ[setting] = "1"
        import numpy  # noqa: F401 -- preload numerical code after namespace setup

        from quail.language.evaluator import Evaluator
        from quail.language.state import State

        inbox: queue.Queue[JSONObject] = queue.Queue(maxsize=2)
        expected = 1

        def request_vectors(texts: list[str]) -> tuple[bytes, ...]:
            assert runner is not None
            # Defer the exception, not the limit latch, until the matching reply
            # is consumed. An interrupt must not split a control frame or leave
            # an old embedding response queued ahead of the next numbered cell.
            # The host still monitors the process and enforces the hard grace.
            runner.exchanging_embeddings = True
            try:
                wire.send(outgoing, EmbeddingRequest(expected, tuple(texts)).to_record(), maximum)
                reply = EmbeddingReply.from_record(inbox.get(), expected, len(texts))
            finally:
                runner.exchanging_embeddings = False
            if runner.expired:
                raise QuailError(runner.expired)
            if reply.error is not None:
                raise QuailError(reply.error.message, reply.error.hint)
            return reply.vectors

        runner = Runner(
            Evaluator(
                State(Path(index), source, session, limits),
                embedding_id=identity,
                embed=request_vectors,
            )
        )
        threading.Thread(
            target=_watch_control, args=(incoming, inbox, maximum), daemon=True
        ).start()
        install_confinement(runner.evaluator.state.connection)
        wire.send(outgoing, {"type": "ready", "confinement": confinement}, maximum)
        while True:
            request = inbox.get()
            if request == {"type": "close"}:
                return 0
            n, code = request.get("n"), request.get("code")
            if (
                request.keys() != {"type", "n", "code"}
                or request["type"] != "run"
                or type(n) is not int
                or n != expected
                or not isinstance(code, str)
            ):
                raise QuailError("Invalid numbered cell request")
            wire.send(outgoing, runner.run(n, code).to_record(), maximum)
            expected += 1
    except BaseException as error:
        wire.send(
            outgoing, {"type": "fatal", "error": _error_info(error, 16384).to_record()}, maximum
        )
        return 1
    finally:
        if runner is not None:
            runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
