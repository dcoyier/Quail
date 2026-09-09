import io

import pytest

from quail import wire
from quail.contracts import QuailError
from quail.language.evaluator import Evaluator
from quail.language.state import State
from quail.prelude import BoundedOutput, Runner, public_namespace


@pytest.fixture
def runner(index, study):
    result = Runner(
        Evaluator(State(index.path, index.source, "review", study.limits)), timers=False
    )
    try:
        yield result
    finally:
        result.close()


def test_persistent_module_supports_dataclasses_closures_and_future_flags(runner):
    first = runner.run(
        1,
        """from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Theme:
    query: str
    later: NotDefinedYet = None

    def predicate(self, within=None):
        found = Field("body").lexical(self.query) > 0
        return found if within is None else found & within

def factory(prefix):
    def label(number):
        return f"{prefix}:{number}"
    return label

theme = Theme("parking")
label = factory("topic")
count(theme.predicate())
""",
    )
    assert first.error is None, first.output
    assert first.output == "1\n"
    second = runner.run(
        2, "class Later:\n    value: StillNotDefined\nprint(label(3))\ncount(theme.predicate())"
    )
    assert second.error is None, second.output
    assert second.output == "topic:3\n1\n"


def test_failed_cell_keeps_python_bindings_and_output_but_rolls_back_tags(runner):
    failed = runner.run(
        1, 'kept = 42\nprint("before")\ntag(None, "attempt", True)\nraise ValueError("bad cell")'
    )
    assert failed.error.type == "ValueError"
    assert "before\n" in failed.output
    assert "cell-1" in failed.output and "quail/prelude.py" not in failed.output
    assert failed.tags == {}
    following = runner.run(2, '(kept, "attempt" in [f["name"] for f in fields()])')
    assert following.output == "(42, False)\n"
    assert following.error is None


def test_output_capture_keeps_a_utf8_prefix_and_counts_omissions():
    output = BoundedOutput(3)
    output.write("éé")
    output.write("tail")
    assert bytes(output.kept) == "é".encode()
    assert output.omitted == 6
    assert "6 UTF-8 bytes omitted" in output.finish()


def test_cell_output_is_bounded_as_it_is_written(runner):
    reply = runner.run(1, 'for _ in range(100):\n    print("é" * 1000)')
    assert reply.error is None
    assert reply.truncated
    assert len(reply.output.encode()) < runner.limits.output_kib * 1024 + 100


def test_public_bindings_are_explicit_and_shadowed_verbs_are_recoverable(runner):
    assert set(public_namespace(runner.evaluator)) == {
        "count",
        "retrieve",
        "values",
        "tag",
        "fields",
        "Field",
        "Random",
        "Expression",
        "Predicate",
        "Entry",
        "QuailError",
        "quail",
        "re",
        "math",
        "statistics",
        "json",
        "itertools",
        "collections",
        "Counter",
    }
    assert runner.run(1, "count = 0\ncount = quail.count\ncount()").output == "2\n"


def test_fragmented_framing_never_accepts_incomplete_records():
    record = {"code": "print('café')\n42", "type": "run", "n": 1}
    encoded = wire.encode(record)
    decoder = wire.Decoder()
    actual = []
    for byte in encoded:
        actual.extend(decoder.feed(bytes([byte])))
    assert actual == [record]
    assert wire.receive(io.BytesIO(encoded)) == record
    with pytest.raises(EOFError):
        wire.receive(io.BytesIO(encoded[:-1]))
    with pytest.raises(QuailError, match="allowance"):
        wire.receive(io.BytesIO(encoded), max_bytes=1)
