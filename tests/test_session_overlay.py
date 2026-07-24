"""Session overlay commit and catalog reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from quail.datasets import import_csv_dataset, open_core_db, source_fields
from quail.session import (
    FieldCreate,
    SessionConflictError,
    ValueDelete,
    ValueWrite,
    analysis_values,
    catalog_fields,
    commit_overlay,
    create_session,
    resolve_scope,
)


def _seed(tmp_path: Path):
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("id,title\ne1,Hello\ne2,World\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    ref = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    scope = resolve_scope(db, session.id, "notes")
    return db, session, scope, ref


def test_catalog_starts_source_only(tmp_path: Path) -> None:
    db, _session, scope, _ref = _seed(tmp_path)
    with db:
        fields = catalog_fields(db, scope)
        assert [(field.name, field.kind) for field in fields] == [("title", "source")]


def test_commit_create_field_and_tag(tmp_path: Path) -> None:
    db, session, scope, ref = _seed(tmp_path)
    with db:
        before_source = source_fields(db, "ws", "notes", ref.version_id)
        new_revision = commit_overlay(
            db,
            scope,
            expected_revision=session.state_revision,
            mutations=[
                FieldCreate("topic"),
                ValueWrite("topic", "e1", "climate"),
            ],
        )
        assert new_revision == 1
        fields = catalog_fields(db, scope)
        assert [(field.name, field.kind) for field in fields] == [
            ("title", "source"),
            ("topic", "analysis"),
        ]
        assert analysis_values(db, scope, "topic") == ["climate", None]
        after_source = source_fields(db, "ws", "notes", ref.version_id)
        assert after_source == before_source
        source_count = db.connection.execute("SELECT COUNT(*) FROM quail_source_values").fetchone()
        assert int(source_count[0]) == 2


def test_source_name_collision_rejected(tmp_path: Path) -> None:
    db, session, scope, _ref = _seed(tmp_path)
    with db:
        with pytest.raises(SessionConflictError, match="collides with source"):
            commit_overlay(
                db,
                scope,
                expected_revision=session.state_revision,
                mutations=[FieldCreate("title")],
            )
        assert catalog_fields(db, scope) == [
            field for field in catalog_fields(db, scope) if field.kind == "source"
        ]
        assert get_revision(db, session.id) == 0


def test_stale_revision_conflicts(tmp_path: Path) -> None:
    db, session, scope, _ref = _seed(tmp_path)
    with db:
        commit_overlay(
            db,
            scope,
            expected_revision=0,
            mutations=[FieldCreate("topic")],
        )
        with pytest.raises(SessionConflictError, match="state_revision"):
            commit_overlay(
                db,
                scope,
                expected_revision=0,
                mutations=[ValueWrite("topic", "e1", "x")],
            )
        assert analysis_values(db, scope, "topic") == [None, None]


def test_skipping_commit_leaves_no_overlay(tmp_path: Path) -> None:
    db, session, scope, _ref = _seed(tmp_path)
    with db:
        # Simulate a failed exec: stage mutations in memory only, never commit.
        staged = [FieldCreate("topic"), ValueWrite("topic", "e1", "climate")]
        del staged
        assert [field.kind for field in catalog_fields(db, scope)] == ["source"]
        assert get_revision(db, session.id) == 0


def test_bindings_round_trip(tmp_path: Path) -> None:
    from quail.analysis.bindings import encode_binding_value

    db, session, scope, _ref = _seed(tmp_path)
    with db:
        binding = encode_binding_value(["e1"])
        revision = commit_overlay(
            db,
            scope,
            expected_revision=0,
            mutations=[FieldCreate("topic")],
            bindings={"selected": binding},
        )
        assert revision == 1
        row = db.connection.execute(
            """
            SELECT value_kind, value_json FROM quail_session_bindings
            WHERE session_id = ? AND name = ?
            """,
            (session.id, "selected"),
        ).fetchone()
        assert row is not None
        assert row[0] == binding.value_kind
        assert '"e1"' in str(row[1])

        revision = commit_overlay(
            db,
            scope,
            expected_revision=1,
            binding_deletes=["selected"],
        )
        assert revision == 2
        gone = db.connection.execute(
            """
            SELECT 1 FROM quail_session_bindings
            WHERE session_id = ? AND name = ?
            """,
            (session.id, "selected"),
        ).fetchone()
        assert gone is None


def test_value_delete_clears_tag(tmp_path: Path) -> None:
    db, session, scope, _ref = _seed(tmp_path)
    with db:
        commit_overlay(
            db,
            scope,
            expected_revision=0,
            mutations=[
                FieldCreate("topic"),
                ValueWrite("topic", "e1", "climate"),
            ],
        )
        commit_overlay(
            db,
            scope,
            expected_revision=1,
            mutations=[ValueDelete("topic", "e1")],
        )
        assert analysis_values(db, scope, "topic") == [None, None]


def get_revision(db, session_id: str) -> int:
    row = db.connection.execute(
        "SELECT state_revision FROM quail_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])
