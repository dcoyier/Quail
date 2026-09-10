"""Actual child-boundary checks, independent of the local Unix-socket adapter."""

import os
import selectors
import signal
import subprocess
import sys
import time

import pytest

from quail import wire
from quail.contracts import EmbeddingReply, EmbeddingRequest, ErrorInfo, embedding_identity
from quail.index import pack_vector


class Child:
    def __init__(self, index, study, scratch, *, cpu=30, embedding_id=None):
        child_in, parent_out = os.pipe()
        parent_in, child_out = os.pipe()
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SQLITE_TMPDIR": str(scratch),
            "QUAIL_CONTROL_IN": str(child_in),
            "QUAIL_CONTROL_OUT": str(child_out),
        }
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "quail.prelude"],
                env=environment,
                pass_fds=(child_in, child_out),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            os.close(child_in)
            os.close(child_out)
        self.incoming = os.fdopen(parent_in, "rb", buffering=0)
        self.outgoing = os.fdopen(parent_out, "wb", buffering=0)
        self.decoder = wire.Decoder()
        self.n = 0
        limits = study.limits.to_record() | {"cpu_seconds": cpu}
        wire.send(
            self.outgoing,
            {
                "type": "bootstrap",
                "index": str(index.path),
                "session": "review",
                "source": index.source.to_record(),
                "limits": limits,
                "embedding_id": embedding_id,
            },
        )
        self.ready = self.receive()

    def receive(self):
        deadline = time.monotonic() + 15
        with selectors.DefaultSelector() as selector:
            selector.register(self.incoming, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(timeout=0.1):
                    continue
                chunk = os.read(self.incoming.fileno(), 65536)
                if not chunk:
                    raise AssertionError(f"Child channel closed, exit={self.process.poll()}")
                records = self.decoder.feed(chunk)
                if records:
                    assert len(records) == 1
                    return records[0]
        raise AssertionError("Child did not respond within the test deadline")

    def submit(self, code):
        self.n += 1
        wire.send(self.outgoing, {"type": "run", "n": self.n, "code": code})

    def execute(self, code):
        self.submit(code)
        return self.receive()

    def close(self):
        self.outgoing.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.incoming.close()


@pytest.fixture
def child(index, study, tmp_path):
    result = Child(index, study, tmp_path)
    try:
        assert result.ready["type"] == "ready", result.ready
        yield result
    finally:
        result.close()


def test_child_keeps_python_state_and_runs_numpy_and_private_fts(child):
    first = child.execute(
        "import numpy as np\nimport xml.etree.ElementTree as ET\nx = np.arange(4)\n"
        "assert np.linalg.norm(np.eye(2)) > 1\n"
        "assert np.isfinite(np.random.default_rng(1).normal(size=3)).all()\n"
        'body = Field("body")\ntag(None,"copy",body.lower())\n'
        'count(Field("copy").lexical("parking") > 0)'
    )
    assert first["error"] is None, first
    assert first["output"] == "1\n"
    second = child.execute('print(x.sum())\nvalues(Field("copy"))')
    assert second["error"] is None, second
    assert second["output"] == "6\n['parking is expensive', 'helpful staff']\n"
    assert child.ready["confinement"] in {"audit", "audit+netns"}


@pytest.mark.parametrize(
    "code",
    [
        'open("no-file", "w")',
        'import os; os.symlink("missing", "no-link")',
        'import os; os.mkfifo("no-fifo")',
        'import sqlite3; sqlite3.connect(":memory:")',
        "import socket; socket.socket()",
        'import subprocess; subprocess.run(["true"])',
        "import ctypes; ctypes.CDLL(None)",
    ],
)
def test_accidental_capabilities_are_denied_and_child_remains_usable(child, code):
    reply = child.execute(code)
    assert reply["type"] == "result", reply
    assert reply["error"]["type"] == "QuailError", reply
    assert reply["tags"] == {}
    assert child.execute("count()")["output"] == "2\n"


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE main.forbidden (x)",
        "ATTACH DATABASE ':memory:' AS forbidden",
        "PRAGMA user_version=3",
        "SELECT load_extension('forbidden')",
    ],
)
def test_sql_authorizer_enforces_main_read_only_even_through_internal_connection(child, statement):
    # Test access to the internal connection is intentional: URI mode=ro alone
    # would still permit ATTACH, and the Python audit hook cannot inspect SQL.
    reply = child.execute(f"quail.count.__self__.state.connection.execute({statement!r})")
    assert reply["error"] is not None, reply
    assert child.execute('tag(None,"allowed", True)')["error"] is None


def test_loss_of_host_pipe_terminates_an_executing_child(child):
    child.submit("while True:\n    pass")
    child.outgoing.close()
    assert child.process.wait(timeout=3) != 0


def test_caught_cpu_interrupt_cannot_commit_tags(index, study, tmp_path):
    child = Child(index, study, tmp_path, cpu=0.05)
    try:
        assert child.ready["type"] == "ready", child.ready
        reply = child.execute(
            "try:\n    while True:\n        pass\nexcept BaseException:\n    kept = 42\n"
            'tag(None,"attempt", True)'
        )
        assert reply["error"]["type"] == "QuailError", reply
        assert "CPU" in reply["error"]["message"]
        assert reply["tags"] == {}
        assert child.execute("kept")["output"] == "42\n"
    finally:
        child.close()


def test_confined_semantics_use_packed_replies_and_keep_prepared_scores(index, study, tmp_path):
    child = Child(index, study, tmp_path, embedding_id=embedding_identity("ollama/test", "v1"))
    try:
        child.submit('score = Field("body").semantic("query"); values(score)')
        request = EmbeddingRequest.from_record(child.receive(), 1)
        assert request.texts == ("Parking is expensive", "Helpful staff")
        wire.send(
            child.outgoing,
            EmbeddingReply(1, (pack_vector([1, 0]), pack_vector([0, 1]))).to_record(),
        )
        query = EmbeddingRequest.from_record(child.receive(), 1)
        assert query.texts == ("query",)
        wire.send(child.outgoing, EmbeddingReply(1, (pack_vector([1, 0]),)).to_record())
        result = child.receive()
        assert result["error"] is None and result["output"] == "[1.0, 0.0]\n", result
        reused = child.execute('tag(score > 0.5, "match", True); retrieve(rank=score)[0][score]')
        assert reused["type"] == "result" and reused["error"] is None
        assert reused["output"] == "1.0\n" and reused["tags"] == {"match": {"a": True}}
    finally:
        child.close()


@pytest.mark.parametrize("interrupted", [False, True])
def test_embedding_failure_or_interrupt_leaves_no_stale_reply(index, study, tmp_path, interrupted):
    child = Child(index, study, tmp_path, embedding_id=embedding_identity("ollama/test", "v1"))
    try:
        child.submit(
            'kept = 7; tag(None, "lost", True); count(Field("body").semantic("query") > 0)'
        )
        request = EmbeddingRequest.from_record(child.receive(), 1)
        if interrupted:
            child.process.send_signal(signal.SIGINT)
            response = EmbeddingReply(1, tuple(pack_vector([1, 0]) for _ in request.texts))
        else:
            response = EmbeddingReply(1, error=ErrorInfo("QuailError", "provider unavailable"))
        wire.send(child.outgoing, response.to_record())
        failed = child.receive()
        assert failed["type"] == "result" and failed["tags"] == {}
        assert ("Wall" if interrupted else "unavailable") in failed["error"]["message"]
        assert child.execute("kept")["output"] == "7\n"
    finally:
        child.close()
