import hashlib

import pytest

from quail import history
from quail.contracts import ErrorInfo, QuailError, canonical_json


def replay(study, session):
    snapshot = history.snapshot(study.session_path(session.name) / "log")
    summary = history.Summary()
    records = list(history.records(snapshot, session, summary))
    return records, summary, snapshot


def test_logical_order_across_runs_includes_failures(study, session, run_factory, cell_factory):
    b = run_factory("b")
    a = run_factory("a")
    b.append(cell_factory(order=1, tags={"topic": {"a": "b"}}))
    a.append(cell_factory(order=1, tags={"topic": {"a": "a"}}))
    a.append(cell_factory(n=2, order=9, error=ErrorInfo("ValueError", "no")))
    records, summary, _ = replay(study, session)
    assert [(header.run, cell.n) for header, cell in records] == [("a", 1), ("b", 1), ("a", 2)]
    assert (summary.runs, summary.cells, summary.failed, summary.max_order) == (2, 3, 1, 9)


@pytest.mark.parametrize("tail", [b'{"n":2}', b"\xe2\x82", b""])
def test_unterminated_tail_is_ignored_without_touching_original(
    study, session, run_factory, cell_factory, tail
):
    log = run_factory("run")
    log.append(cell_factory())
    log.close()
    with log.path.open("ab") as stream:
        stream.write(tail)
    before = log.path.read_bytes()
    records, summary, snapshot = replay(study, session)
    assert len(records) == 1
    assert bool(summary.warnings) == bool(tail)
    assert snapshot.hashes[log.path.name] == "sha256:" + hashlib.sha256(before).hexdigest()
    assert log.path.read_bytes() == before


def test_empty_and_unterminated_headers_do_not_strand_a_session(study, session):
    directory = study.session_path(session.name) / "log"
    directory.mkdir(parents=True)
    (directory / "empty.jsonl").write_bytes(b"")
    (directory / "partial.jsonl").write_bytes(b'{"format":')
    records, summary, _ = replay(study, session)
    assert not records
    assert len(summary.warnings) == 2


@pytest.mark.parametrize("bad", [b"not json\n", b"{}\n", b"\xff\n"])
def test_complete_bad_line_is_fatal_even_with_later_valid_records(
    study, session, run_factory, cell_factory, bad
):
    log = run_factory("run")
    log.append(cell_factory())
    log.close()
    with log.path.open("ab") as stream:
        stream.write(bad)
        stream.write((canonical_json(cell_factory(n=3, order=3).to_record()) + "\n").encode())
    with pytest.raises(QuailError, match=r"run.jsonl:3"):
        replay(study, session)


def test_uncertain_fsync_poisoned_writer_cannot_append_contradictory_result(
    run_factory, cell_factory, monkeypatch
):
    log = run_factory("run")

    def fail(_):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(history.os, "fsync", fail)
    with pytest.raises(OSError, match="fsync"):
        log.append(cell_factory())
    with pytest.raises(QuailError, match="uncertain"):
        log.append(cell_factory(error=ErrorInfo("IOError", "no")))
