"""Session CSV export for serve-host process snapshots."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from quail.analysis.errors import QuailRuntimeError
from quail.datasets import import_csv_dataset, open_core_db
from quail.datasets.csv_import import load_csv_dataset
from quail.session import (
    FieldCreate,
    ValueWrite,
    commit_overlay,
    create_session,
    export_session_csv,
    resolve_scope,
)
from quail.session.errors import SessionSyntaxError


def _seed(tmp_path: Path):
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("id,title\ne1,Hello\ne2,World\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    ref = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    scope = resolve_scope(db, session.id, "notes")
    return db, session, scope, ref


def test_export_includes_source_and_sparse_tags(tmp_path: Path) -> None:
    db, session, scope, _ref = _seed(tmp_path)
    dest = tmp_path / "exports"
    with db:
        commit_overlay(
            db,
            scope,
            expected_revision=session.state_revision,
            mutations=[
                FieldCreate("topic"),
                ValueWrite("topic", "e1", "climate"),
            ],
        )
        result = export_session_csv(
            db,
            session_id=session.id,
            dataset_id="notes",
            dest_dir=dest,
        )
    assert result.row_count == 2
    assert result.columns == ("id", "title", "topic")
    assert result.path.parent == dest.resolve()
    assert result.path.name == f"notes.{session.id}.csv"
    rows = list(csv.DictReader(result.path.open(encoding="utf-8")))
    assert rows == [
        {"id": "e1", "title": "Hello", "topic": "climate"},
        {"id": "e2", "title": "World", "topic": ""},
    ]
    loaded = load_csv_dataset(result.path)
    assert loaded.field_names == ("title", "topic")
    by_id = {entry["id"]: entry for entry in loaded.entries}
    assert by_id["e1"]["topic"] == "climate"
    assert "topic" not in by_id["e2"]


def test_export_writes_non_string_tags_as_canonical_json(tmp_path: Path) -> None:
    db, session, scope, _ref = _seed(tmp_path)
    with db:
        commit_overlay(
            db,
            scope,
            expected_revision=session.state_revision,
            mutations=[
                FieldCreate("flags"),
                ValueWrite("flags", "e1", ["a", "b"]),
            ],
        )
        result = export_session_csv(
            db,
            session_id=session.id,
            dataset_id="notes",
            dest_dir=tmp_path / "exports",
        )
    rows = list(csv.DictReader(result.path.open(encoding="utf-8")))
    assert rows[0]["flags"] == '["a","b"]'


def test_export_source_only_when_session_has_no_tags(tmp_path: Path) -> None:
    db, session, _scope, _ref = _seed(tmp_path)
    with db:
        result = export_session_csv(
            db,
            session_id=session.id,
            dataset_id="notes",
            dest_dir=tmp_path / "exports",
        )
    assert result.columns == ("id", "title")
    rows = list(csv.DictReader(result.path.open(encoding="utf-8")))
    assert [row["id"] for row in rows] == ["e1", "e2"]


def test_export_rejects_analysis_field_named_id(tmp_path: Path) -> None:
    db, session, scope, _ref = _seed(tmp_path)
    with db:
        commit_overlay(
            db,
            scope,
            expected_revision=session.state_revision,
            mutations=[FieldCreate("id")],
        )
        with pytest.raises(SessionSyntaxError, match="analysis field 'id'"):
            export_session_csv(
                db,
                session_id=session.id,
                dataset_id="notes",
                dest_dir=tmp_path / "exports",
            )


def test_export_rejects_dataset_id_that_is_not_a_filename(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("id,title\ne1,Hello\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    with db:
        import_csv_dataset(db, "ws", "foo/bar", csv_path, activate=True)
        session = create_session(db, "ws")
        with pytest.raises(SessionSyntaxError, match="filename"):
            export_session_csv(
                db,
                session_id=session.id,
                dataset_id="foo/bar",
                dest_dir=tmp_path / "exports",
            )


def test_export_quotes_commas_and_roundtrips_import(tmp_path: Path) -> None:
    db, session, scope, _ref = _seed(tmp_path)
    with db:
        commit_overlay(
            db,
            scope,
            expected_revision=session.state_revision,
            mutations=[
                FieldCreate("note"),
                ValueWrite("note", "e1", 'say "hi", please'),
            ],
        )
        result = export_session_csv(
            db,
            session_id=session.id,
            dataset_id="notes",
            dest_dir=tmp_path / "exports",
        )
    loaded = load_csv_dataset(result.path)
    by_id = {entry["id"]: entry for entry in loaded.entries}
    assert by_id["e1"]["note"] == 'say "hi", please'


def test_export_deletes_temp_when_over_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quail.session.export.export as export_mod

    monkeypatch.setattr(export_mod, "MAX_CSV_BYTES", 1)
    db, session, _scope, _ref = _seed(tmp_path)
    dest = tmp_path / "exports"
    with db:
        with pytest.raises(QuailRuntimeError, match="byte import limit"):
            export_session_csv(
                db,
                session_id=session.id,
                dataset_id="notes",
                dest_dir=dest,
            )
    assert list(dest.glob("*.tmp")) == []
    assert list(dest.glob("*.csv")) == []

