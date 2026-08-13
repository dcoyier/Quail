"""Turso-backed CSV import and immutable source reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from quail.datasets import (
    DatasetConflictError,
    DatasetSyntaxError,
    import_csv_dataset,
    load_csv_dataset,
    open_core_db,
    source_entries,
    source_fields,
    source_values,
)


def _write_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_import_csv_and_read_source(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "notes.csv",
        "id,title,body\ne1,Hello,world\ne2,Second,\n",
    )
    with open_core_db(tmp_path / "core.turso") as db:
        ref = import_csv_dataset(
            db,
            "ws",
            "notes",
            csv_path,
            name="Notes",
            activate=True,
        )
        assert ref.active_version_id == ref.version_id
        assert ref.version_id.startswith("csv_")
        assert ref.name == "Notes"

        fields = source_fields(db, "ws", "notes", ref.version_id)
        assert [field.name for field in fields] == ["title", "body"]
        entries = source_entries(db, "ws", "notes", ref.version_id)
        assert [entry.id for entry in entries] == ["e1", "e2"]
        titles = source_values(db, "ws", "notes", ref.version_id, "title")
        bodies = source_values(db, "ws", "notes", ref.version_id, "body")
        assert titles == ["Hello", "Second"]
        assert bodies == ["world", None]


def test_reimport_identical_csv_is_idempotent(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "notes.csv",
        "id,title\ne1,Hello\n",
    )
    with open_core_db(tmp_path / "core.turso") as db:
        first = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
        second = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
        assert first.version_id == second.version_id
        assert first.content_hash == second.content_hash
        count = db.connection.execute(
            """
            SELECT COUNT(*) FROM quail_dataset_versions
            WHERE workspace_id = ? AND dataset_id = ?
            """,
            ("ws", "notes"),
        ).fetchone()
        assert int(count[0]) == 1


def test_changed_csv_creates_new_version_and_keeps_old(tmp_path: Path) -> None:
    first_csv = _write_csv(
        tmp_path / "v1.csv",
        "id,title\ne1,Hello\n",
    )
    second_csv = _write_csv(
        tmp_path / "v2.csv",
        "id,title\ne1,Hello\ne2,More\n",
    )
    with open_core_db(tmp_path / "core.turso") as db:
        first = import_csv_dataset(db, "ws", "notes", first_csv, activate=True)
        second = import_csv_dataset(db, "ws", "notes", second_csv, activate=True)
        assert first.version_id != second.version_id
        assert second.active_version_id == second.version_id

        old_titles = source_values(db, "ws", "notes", first.version_id, "title")
        new_titles = source_values(db, "ws", "notes", second.version_id, "title")
        assert old_titles == ["Hello"]
        assert new_titles == ["Hello", "More"]


def test_bad_csv_shapes_raise(tmp_path: Path) -> None:
    missing_id = _write_csv(tmp_path / "missing_id.csv", "title\nx\n")
    duplicate_header = _write_csv(tmp_path / "dup_header.csv", "id,id\na,b\n")
    duplicate_id = _write_csv(tmp_path / "dup_id.csv", "id,title\ne1,a\ne1,b\n")
    short_row = _write_csv(tmp_path / "short.csv", "id,title,body\ne1,Hello\n")
    extra_row = _write_csv(tmp_path / "extra.csv", "id,title\ne1,Hello,world\n")

    with pytest.raises(DatasetSyntaxError, match="id column"):
        load_csv_dataset(missing_id)
    with pytest.raises(DatasetSyntaxError, match="unique"):
        load_csv_dataset(duplicate_header)
    with pytest.raises(DatasetSyntaxError, match="duplicates id"):
        load_csv_dataset(duplicate_id)
    with pytest.raises(DatasetSyntaxError, match="fewer values than headers"):
        load_csv_dataset(short_row)
    with pytest.raises(DatasetSyntaxError, match="more values than headers"):
        load_csv_dataset(extra_row)


def test_version_identity_conflict(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "notes.csv",
        "id,title\ne1,Hello\n",
    )
    with open_core_db(tmp_path / "core.turso") as db:
        ref = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
        # Corrupt stored hash while keeping the same version id primary key.
        db.connection.execute(
            """
            UPDATE quail_dataset_versions
            SET content_hash = 'deadbeef'
            WHERE workspace_id = ? AND dataset_id = ? AND id = ?
            """,
            ("ws", "notes", ref.version_id),
        )
        db.connection.commit()
        with pytest.raises(DatasetConflictError, match="conflicts"):
            import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
