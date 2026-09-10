"""The local adapter: one detached host per session, one request per connection.

The host's main thread owns Kernel. Bounded connection workers validate requests,
serve immutable status, and deliver output; they never touch its SQLite state.
A single admission slot rejects competing work instead of queuing cells.
"""

from __future__ import annotations

import os
import queue
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from quail import wire
from quail.contracts import ErrorInfo, JSONObject, QuailError, digest_bytes, json_object
from quail.project import Project, load, load_session, validate_name

if TYPE_CHECKING:
    from quail.kernel import CellIdentity, Kernel

PROTOCOL = 2
# bind/connect have no dir_fd variant. Restrict the temporary cwd change to this
# local adapter and serialize it; every project operation uses resolved paths.
_socket_directory = threading.Lock()


@dataclass(frozen=True)
class Request:
    operation: Literal["exec", "reset", "close", "status"]
    project: str
    session: str
    code: str | None = None
    dataset: str | None = None
    fork_from: str | None = None

    def to_record(self) -> JSONObject:
        return {
            "protocol": PROTOCOL,
            "op": self.operation,
            "project": self.project,
            "session": self.session,
            "code": self.code,
            "dataset": self.dataset,
            "fork_from": self.fork_from,
        }

    @classmethod
    def from_record(cls, value: JSONObject, project: Project, session: str) -> Request:
        if value.keys() != {"protocol", "op", "project", "session", "code", "dataset", "fork_from"}:
            raise QuailError("Invalid local request fields")
        operation = value["op"]
        if not isinstance(operation, str) or operation not in {"exec", "reset", "close", "status"}:
            raise QuailError("Unknown local operation")
        if value["project"] != str(project.root) or value["session"] != session:
            raise QuailError("Local endpoint belongs to another project or session")
        if operation != "close" and (
            type(value["protocol"]) is not int or value["protocol"] != PROTOCOL
        ):
            raise QuailError(
                "Running host uses an incompatible protocol", "Explicitly close it before reopening"
            )
        code, dataset, fork_from = value["code"], value["dataset"], value["fork_from"]
        if any(
            item is not None and not isinstance(item, str) for item in (code, dataset, fork_from)
        ):
            raise QuailError("Invalid code or session arguments")
        if operation == "exec":
            if not isinstance(code, str):
                raise QuailError("An exec request requires cell text")
        elif any(item is not None for item in (code, dataset, fork_from)):
            raise QuailError("Only exec accepts code, dataset, or fork arguments")
        assert operation in {"exec", "reset", "close", "status"}
        assert dataset is None or isinstance(dataset, str)
        assert fork_from is None or isinstance(fork_from, str)
        assert code is None or isinstance(code, str)
        return cls(
            cast(Literal["exec", "reset", "close", "status"], operation),
            str(project.root),
            session,
            code,
            dataset,
            fork_from,
        )


def _endpoint(project: Project, session: str) -> Path:
    validate_name(session, "session")
    name = digest_bytes(session.encode("utf-8")).removeprefix("sha256:")
    return project.path(".quail", "run", name, "kernel.sock")


@contextmanager
def _relative(path: Path) -> Generator[str, None, None]:
    with _socket_directory:
        previous = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.chdir(path.parent)
            yield path.name
        finally:
            os.fchdir(previous)
            os.close(previous)


def _connect(path: Path) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(1)
        with _relative(path) as name:
            connection.connect(name)
        return connection
    except BaseException:
        connection.close()
        raise


def _owned(project: Project, session: str) -> bool:
    try:
        with project.lock("session", session):
            return False
    except QuailError as error:
        if isinstance(error.__cause__, BlockingIOError):
            return True
        raise


def _existing(project: Project, session: str) -> socket.socket | None:
    try:
        return _connect(_endpoint(project, session))
    except (FileNotFoundError, ConnectionRefusedError) as error:
        if _owned(project, session):
            raise QuailError(
                "Session owner is initializing or its endpoint is unavailable",
                "Wait for startup or inspect the owner; its endpoint has been preserved",
            ) from error
        return None
    except OSError as error:
        raise QuailError(f"Cannot connect to session {session!r}: {error}") from error


def _start(
    project: Project, session: str, dataset: str | None, fork_from: str | None
) -> JSONObject:
    """Wait for readiness or an actual startup failure, however long replay takes."""
    with ExitStack() as channels:
        config_read, config_write = os.pipe()
        child_input = channels.enter_context(os.fdopen(config_read, "rb"))
        outgoing = channels.enter_context(os.fdopen(config_write, "wb"))
        ready_read, ready_write = os.pipe()
        incoming = channels.enter_context(os.fdopen(ready_read, "rb"))
        child_output = channels.enter_context(os.fdopen(ready_write, "wb"))
        environment = dict(os.environ)
        environment.update(QUAIL_LOCAL_IN=str(config_read), QUAIL_LOCAL_OUT=str(ready_write))
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "quail.local"],
                env=environment,
                pass_fds=(config_read, ready_write),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            child_input.close()
            child_output.close()
        try:
            wire.send(
                outgoing,
                {
                    "project": str(project.root),
                    "session": session,
                    "dataset": dataset,
                    "fork_from": fork_from,
                },
            )
            result = wire.receive(incoming)
        except (OSError, EOFError) as error:
            raise QuailError(
                "Local host exited during startup",
                "Inspect session history before resubmitting code",
            ) from error
    if result.get("type") == "error":
        process.wait(timeout=5)
        _raise_error(result)
    ready = json_object(result.pop("value", None), "host readiness result")
    if result != {
        "type": "ready",
        "protocol": PROTOCOL,
        "project": str(project.root),
        "session": session,
    }:
        raise QuailError("Local host returned an invalid readiness record")
    return ready


def _raise_error(record: JSONObject) -> None:
    error = ErrorInfo.from_record(json_object(record.get("error")))
    message = error.message if error.type == "QuailError" else f"{error.type}: {error.message}"
    raise QuailError(message, error.hint)


def _ready_result(owner: Kernel) -> JSONObject:
    """Use the host's applied state and notices after opening or resetting."""
    return {
        **owner.runtime.to_record(),
        "session": owner.session.name,
        "warnings": list(owner.opening_warnings),
    }


def call(
    project: Project,
    session: str,
    operation: Literal["exec", "reset", "close"],
    *,
    code: str | None = None,
    dataset: str | None = None,
    fork_from: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> JSONObject:
    validate_name(session, "session")
    if operation == "reset":
        load_session(project, session)  # Reset never invents a new session.
    connection = _existing(project, session)
    if connection is None and operation == "close":
        return {"closed": True, "session": session, "state": "stopped"}
    if connection is None:
        ready = _start(project, session, dataset, fork_from)
        if operation == "reset":
            # Opening already created the requested fresh run. Return its ready
            # result without resetting again or depending on a later status call.
            return {"reset": True, **ready}
        connection = _existing(project, session)
        if connection is None:
            raise QuailError("Host stopped after startup; no cell has been submitted")
        fork_from = None  # Creation already validated/copied it; this is now an existing session.
    request = Request(operation, str(project.root), session, code, dataset, fork_from)
    # Validate before any send, even for callers other than argparse.
    Request.from_record(request.to_record(), project, session)
    accepted: JSONObject | None = None
    with connection, connection.makefile("rwb", buffering=0) as stream:
        wire.send(stream, request.to_record())
        connection.settimeout(None)  # Accepted work is governed by Kernel/provider limits.
        try:
            while True:
                record = wire.receive(stream)
                kind = record.get("type")
                if kind == "accepted":
                    accepted = record
                elif kind == "progress" and isinstance(record.get("message"), str):
                    if progress is not None:
                        progress(str(record["message"]))
                elif kind == "result":
                    return json_object(record.get("value"), "local result")
                elif kind == "error":
                    _raise_error(record)
                else:
                    raise QuailError("Invalid local host response")
        except (OSError, EOFError) as error:
            identity = f" {accepted.get('run')}/{accepted.get('cell')}" if accepted else ""
            raise QuailError(
                f"Lost the session host's response{identity}; the cell may have committed",
                "Inspect runtime status and run logs; do not resubmit automatically",
            ) from error


def runtime(project: Project, session: str) -> JSONObject:
    """Inspect independently of source opening; this never starts a host."""
    try:
        connection = _existing(project, session)
        if connection is None:
            return {"state": "stopped"}
        with connection, connection.makefile("rwb", buffering=0) as stream:
            wire.send(stream, Request("status", str(project.root), session).to_record())
            record = wire.receive(stream)
            if record.get("type") == "error":
                _raise_error(record)
            if record.get("type") != "result":
                raise QuailError("Invalid runtime status response")
            return json_object(record.get("value"))
    except (OSError, EOFError, QuailError) as error:
        return {"state": "unavailable", "reason": str(error)}


class _Delivery:
    """A bounded observer mailbox; a slow/disconnected client cannot stall commits."""

    def __init__(self) -> None:
        self.events: queue.Queue[JSONObject] = queue.Queue(maxsize=8)
        self.done = threading.Event()

    def accepted(self, identity: CellIdentity) -> None:
        self.emit({"type": "accepted", **identity.to_record()})

    def progress(self, message: str) -> None:
        self.emit({"type": "progress", "message": message})

    def emit(self, record: JSONObject) -> None:
        if self.done.is_set():
            return
        try:
            self.events.put_nowait(record)
        except queue.Full:
            # Progress is disposable. Preserve the final response by dropping an
            # old notification, without blocking the execution owner.
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass  # The delivery thread consumed it between these operations.
            self.events.put_nowait(record)

    def forward(self, stream: wire.Writer) -> None:
        try:
            while True:
                record = self.events.get()
                wire.send(stream, record)
                if record.get("type") in {"result", "error"}:
                    return
        finally:
            self.done.set()


@dataclass(frozen=True)
class _Work:
    request: Request
    delivery: _Delivery


class _Server:
    def __init__(self, owner: Kernel) -> None:
        self.owner = owner
        self.endpoint = _endpoint(owner.project, owner.session.name)
        self.admission = threading.Lock()
        self.busy = False
        self.stopping = threading.Event()
        self.work: queue.Queue[_Work] = queue.Queue(maxsize=1)
        self.connections = threading.BoundedSemaphore(16)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # open_session already holds the lifetime lock. Only that owner may
            # replace a stale endpoint, and only after the Kernel is ready.
            self.endpoint.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(self.endpoint.parent, 0o700)
            self.endpoint.unlink(missing_ok=True)
            with _relative(self.endpoint) as name:
                self.listener.bind(name)
            os.chmod(self.endpoint, 0o600)
            status = self.endpoint.stat()
            self.identity = (status.st_dev, status.st_ino)
            self.listener.listen(16)
            self.listener.settimeout(0.2)
        except BaseException:
            self.listener.close()
            raise
        self.thread = threading.Thread(target=self._listen, name="quail-listener", daemon=True)
        self.thread.start()

    def _listen(self) -> None:
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self.stopping.is_set():
                    return
                raise
            if not self.connections.acquire(blocking=False):
                connection.close()
                continue
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(5)  # Incomplete frames are never accepted work.
            with connection, connection.makefile("rwb", buffering=0) as stream:
                try:
                    request = Request.from_record(
                        wire.receive(stream), self.owner.project, self.owner.session.name
                    )
                    connection.settimeout(1)
                    if request.operation == "status":
                        status = self.owner.runtime.to_record()
                        with self.admission:
                            if self.busy and status["state"] == "idle":
                                status["state"] = "busy"
                        wire.send(stream, {"type": "result", "value": status})
                        return
                    with self.admission:
                        if self.stopping.is_set():
                            raise QuailError("Session is closing")
                        if self.busy:
                            raise QuailError(
                                "Kernel is busy", "Wait for the accepted cell to complete"
                            )
                        if (
                            request.dataset is not None
                            and request.dataset != self.owner.session.dataset
                        ):
                            raise QuailError("Dataset does not match the session")
                        if request.fork_from is not None:
                            raise QuailError("An existing session cannot specify fork_from")
                        delivery = _Delivery()
                        self.busy = True
                        self.work.put_nowait(_Work(request, delivery))
                    delivery.forward(stream)
                except (QuailError, OSError, EOFError) as error:
                    try:
                        wire.send(
                            stream,
                            {"type": "error", "error": ErrorInfo.from_exception(error).to_record()},
                        )
                    except OSError:
                        pass
        finally:
            connection.close()
            self.connections.release()

    def stop(self) -> None:
        if not self.stopping.is_set():
            self.stopping.set()
            # Remove the endpoint while the Kernel still holds the session lock.
            # No cleanup after releasing it can unlink a successor's socket.
            with ExitStack() as ownership:
                # A persistence failure can already have closed Kernel. Reacquire
                # authority before cleanup, and never remove a successor endpoint.
                try:
                    if self.owner.runtime.state == "stopped":
                        ownership.enter_context(
                            self.owner.project.lock("session", self.owner.session.name)
                        )
                    status = self.endpoint.stat()
                    if (status.st_dev, status.st_ino) == self.identity:
                        self.endpoint.unlink()
                except FileNotFoundError:
                    pass
                except QuailError as error:
                    if not isinstance(error.__cause__, BlockingIOError):
                        raise
            self.listener.close()
            self.thread.join(timeout=2)

    def run(self) -> None:
        try:
            while True:
                work = self.work.get()
                request, delivery = work.request, work.delivery
                closing = request.operation == "close"
                try:
                    if request.operation == "exec":
                        assert request.code is not None

                        result = self.owner.exec(
                            request.code,
                            on_accept=delivery.accepted,
                            on_progress=delivery.progress,
                        ).to_record()
                    elif request.operation == "reset":
                        self.owner.reset()
                        result = {"reset": True, **_ready_result(self.owner)}
                    else:
                        self.stop()
                        self.owner.close()
                        result = {
                            "closed": True,
                            "session": self.owner.session.name,
                            "state": "stopped",
                        }
                    delivery.emit({"type": "result", "value": result})
                except BaseException as error:
                    delivery.emit(
                        {"type": "error", "error": ErrorInfo.from_exception(error).to_record()}
                    )
                    closing = closing or self.owner.runtime.state == "stopped"
                finally:
                    with self.admission:
                        self.busy = False
                if closing:
                    delivery.done.wait(timeout=2)
                    return
        finally:
            self.stop()
            self.owner.close()


def main() -> int:
    """Private module entry point launched by _start, never a user command."""
    from quail.service import open_session

    incoming = os.fdopen(int(os.environ.pop("QUAIL_LOCAL_IN")), "rb")
    outgoing = os.fdopen(int(os.environ.pop("QUAIL_LOCAL_OUT")), "wb")
    owner = None
    server = None
    try:
        with incoming:
            config = wire.receive(incoming)
        root, session = config.get("project"), config.get("session")
        dataset, fork_from = config.get("dataset"), config.get("fork_from")
        if not isinstance(root, str) or not isinstance(session, str):
            raise QuailError("Invalid local startup scope")
        if dataset is not None and not isinstance(dataset, str):
            raise QuailError("Invalid startup dataset")
        if fork_from is not None and not isinstance(fork_from, str):
            raise QuailError("Invalid fork source")
        project = load(Path(root))
        owner = open_session(project, session, dataset, fork_from)
        server = _Server(owner)
        wire.send(
            outgoing,
            {
                "type": "ready",
                "protocol": PROTOCOL,
                "project": str(project.root),
                "session": session,
                "value": _ready_result(owner),
            },
        )
        outgoing.close()
        server.run()
        return 0
    except BaseException as error:
        if not outgoing.closed:
            wire.send(
                outgoing, {"type": "error", "error": ErrorInfo.from_exception(error).to_record()}
            )
        return 1
    finally:
        outgoing.close()
        if server is not None:
            server.stop()
        if owner is not None:
            owner.close()


if __name__ == "__main__":
    raise SystemExit(main())
