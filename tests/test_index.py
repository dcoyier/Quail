import dataclasses
import sqlite3

import pytest

from quail import history
from quail.contracts import QuailError, digest_bytes
from quail.index import Index, transaction


def test_import_hashes_exact_bytes_and_preserves_text(study, tmp_path):
    raw = b'\xef\xbb\xbfid,weird " header,number,empty\r\na,  leading text,001,""\r\n'
    config = study.dataset("notes")
    config.source.write_bytes(raw)
    with Index.build(tmp_path / "index", config) as index:
        assert index.source.hash == digest_bytes(raw)
        assert index.source.fields == ("id", 'weird " header', "number", "empty")
        assert index.connection.execute("SELECT * FROM entries").fetchall() == [
            ("a", "  leading text", "001", None)
        ]
        assert index.source.present == (1, 1, 1, 0)


@pytest.mark.parametrize(
    "csv", ["id,Body,body\na,b,c\n", "id,rowid\na,b\n", "id,body\na,x\na,y\n", "id,body\na\n"]
)
def test_invalid_csv_never_replaces_an_existing_index(study, index, tmp_path, csv):
    study.dataset("notes").source.write_text(csv)
    with pytest.raises(QuailError):
        Index.build(tmp_path / "replacement", study.dataset("notes"))
    assert index.connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 2


def test_replay_tracks_only_final_orphans_and_restores_returning_ids(
    study, index, session, run_factory, cell_factory, tmp_path
):
    log = run_factory("run")
    log.append(cell_factory(tags={"topic": {"a": True, "missing": "retained", "cleared": 1}}))
    log.append(cell_factory(n=2, order=2, tags={"topic": {"cleared": None}}))
    snapshot = history.snapshot(log.path.parent)
    applied = index.synchronize(session, snapshot)
    assert applied.orphans == 1
    assert index.connection.execute("SELECT entry,value FROM tags").fetchall() == [("a", "true")]
    assert index.connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError):
        index.connection.execute("INSERT INTO tags VALUES ('review','missing','x','1')")
    study.dataset("notes").source.write_text("id,body\na,Parking\nmissing,Returned\n")
    with Index.build(tmp_path / "rebuilt", study.dataset("notes")) as rebuilt:
        assert rebuilt.synchronize(session, snapshot).orphans == 0
        assert rebuilt.connection.execute(
            "SELECT value FROM tags WHERE entry='missing'"
        ).fetchone() == ('"retained"',)


def test_matching_history_reuses_validation_and_invalid_later_history_never_publishes(
    study, index, session, run_factory, cell_factory, monkeypatch
):
    log = run_factory("run")
    log.append(cell_factory(tags={"topic": {"a": "original"}}))
    old = history.snapshot(log.path.parent)
    index.synchronize(session, old)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            history, "records", lambda *args: pytest.fail("matching history must not replay")
        )
        assert index.synchronize(session, old).summary.cells == 1
    log.append(cell_factory(n=2, order=2, tags={"topic": {"a": "new"}}))
    log.close()
    with log.path.open("ab") as stream:
        stream.write(b"bad complete line\n")
    with pytest.raises(QuailError):
        index.synchronize(session, history.snapshot(log.path.parent))
    assert index.applied(session.name).digest == old.digest
    assert index.connection.execute("SELECT value FROM tags").fetchone() == ('"original"',)


def test_generated_identity_cannot_follow_source_edits(study, tmp_path):
    config = study.dataset("notes")
    config.source.write_text("body\nFirst\nSecond\n")
    with Index.build(tmp_path / "first", config) as first:
        from quail.project import SessionMetadata

        session = SessionMetadata("s", "notes", first.source.version, history.now(), None)
        assert first.connection.execute("SELECT id FROM entries ORDER BY rowid").fetchall() == [
            ("row-000001",),
            ("row-000002",),
        ]
    config.source.write_text("body\nSecond\nFirst\n")
    with (
        Index.build(tmp_path / "second", config) as second,
        pytest.raises(QuailError, match="generated"),
    ):
        second.compatible(session)
    config.source.write_text("id,body\nrow-000002,Second\nrow-000001,First\n")
    with Index.build(tmp_path / "stable", config) as stable:
        stable.compatible(session)


def test_transaction_owner_rolls_back_and_nested_owner_is_rejected(index):
    with pytest.raises(RuntimeError, match="nested"), transaction(index.connection):
        index.connection.execute("INSERT INTO tags VALUES ('s','a','x','1')")
        with transaction(index.connection):
            pass
    assert index.connection.execute("SELECT * FROM tags").fetchall() == []


def test_source_tag_conflict_is_scoped_to_affected_session(
    index, session, run_factory, cell_factory
):
    log = run_factory("run")
    log.append(cell_factory(tags={"body": {"a": "collision"}}))
    with pytest.raises(QuailError, match="conflict"):
        index.synchronize(session, history.snapshot(log.path.parent))
    other = dataclasses.replace(session, name="other")
    assert index.synchronize(other, history.Snapshot(())).summary.cells == 0
