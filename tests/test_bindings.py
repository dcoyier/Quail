"""Session binding encode/decode and end-to-end persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from quail.analysis.bindings import (
    decode_binding_value,
    encode_binding_value,
    stamp_encoded_bindings,
    validate_binding_fields,
)
from quail.analysis.entry import make_entry
from quail.analysis.errors import QuailFieldError, QuailRuntimeError, QuailScopeError
from quail.analysis.exec_host import exec_script
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import G0
from quail.analysis.operations import Length, Value
from quail.analysis.unit import Unit
from quail.datasets import import_csv_dataset, open_core_db
from quail.session import create_session, get_session
from quail.session.overlay import (
    commit_overlay,
    ensure_scope,
    load_bindings,
    resolve_scope,
)


def _seed(tmp_path: Path):
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,title,body\ne1,Hello,hydrangea care tips\ne2,Other,climate notes\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    return db, session


def test_binding_codec_scalars_and_reject() -> None:
    encoded = encode_binding_value(None)
    assert decode_binding_value(encoded.value_kind, encoded.value) is None
    encoded = encode_binding_value({"b": 1, "a": 2})
    decoded = decode_binding_value(encoded.value_kind, encoded.value)
    assert list(decoded.keys()) == ["b", "a"]
    with pytest.raises(QuailRuntimeError):
        encode_binding_value((1, 2))
    with pytest.raises(QuailRuntimeError):
        encode_binding_value({1, 2})
    with pytest.raises(QuailRuntimeError):
        encode_binding_value(float("nan"))


def test_binding_codec_symbolic_round_trip() -> None:
    field = Field("content")
    unit = Unit("entries", field)
    expression = Expression(field, Value(), Length())
    entry = make_entry("e1", dataset_id="docs", dataset_version_id="v1")
    for value in (field, unit, expression, entry, [entry]):
        encoded = encode_binding_value(value)
        decoded = decode_binding_value(encoded.value_kind, encoded.value)
        again = encode_binding_value(decoded)
        assert again == encoded


def test_validate_binding_fields_calls_check_on_nested_field() -> None:
    seen: list[Field] = []

    def check(field: Field) -> None:
        seen.append(field)

    validate_binding_fields(
        Unit("values", Field("body", "source")),
        check,
    )
    assert seen == [Field("body", "source")]

    seen.clear()
    validate_binding_fields(Field("ghost", "analysis"), check)
    assert seen == [Field("ghost", "analysis")]


def test_bindings_persist_and_delete_across_exec(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        first = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code='topic = "climate"\nprint(topic)\n',
        )
        assert first.printed_output == "climate\n"
        loaded = load_bindings(db, session.id)
        assert "topic" in loaded
        assert decode_binding_value(loaded["topic"].value_kind, loaded["topic"].value) == "climate"

        second = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=first.state_revision,
            code="print(topic)\ndel topic\n",
        )
        assert second.printed_output == "climate\n"
        assert load_bindings(db, session.id) == {}


def test_unpersistable_binding_rolls_back(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        with pytest.raises(QuailRuntimeError, match="Cannot persist|set"):
            exec_script(
                db,
                session_id=session.id,
                dataset_id="notes",
                expected_revision=0,
                code='create_field("topic")\ntag(G0, Field("topic"), "x")\nbad = {1}\n',
            )
        session_row = get_session(db, session.id)
        assert session_row is not None and session_row.state_revision == 0
        assert load_bindings(db, session.id) == {}


def test_wrong_kind_field_binding_rejected_at_commit(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        with pytest.raises(QuailFieldError, match="registered as source, not analysis"):
            exec_script(
                db,
                session_id=session.id,
                dataset_id="notes",
                expected_revision=0,
                code='bad = Field("body", "analysis")\nprint(bad.kind)\n',
            )
        session_row = get_session(db, session.id)
        assert session_row is not None and session_row.state_revision == 0
        assert load_bindings(db, session.id) == {}


def test_correct_kind_and_none_field_bindings_round_trip(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        first = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code=(
                'source_body = Field("body", "source")\n'
                'lazy_body = Field("body")\n'
                "print(source_body.kind, lazy_body.kind)\n"
            ),
        )
        assert first.printed_output == "source None\n"
        loaded = load_bindings(db, session.id)
        assert set(loaded) == {"source_body", "lazy_body"}

        second = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=first.state_revision,
            code="print(source_body.kind, lazy_body.name)\n",
        )
        assert second.printed_output == "source body\n"


def test_wrong_kind_field_binding_does_not_block_restore(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        scope = resolve_scope(db, session.id, "notes")
        ensure_scope(db, scope)
        revision = commit_overlay(
            db,
            scope,
            expected_revision=0,
            bindings={"bad": encode_binding_value(Field("body", "analysis"))},
        )
        assert "bad" in load_bindings(db, session.id)

        outcome = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=revision,
            code="print(1)\n",
        )
        assert outcome.printed_output == "1\n"
        assert "bad" in load_bindings(db, session.id)

        with pytest.raises(QuailFieldError, match="registered as source, not analysis"):
            exec_script(
                db,
                session_id=session.id,
                dataset_id="notes",
                expected_revision=outcome.state_revision,
                code=(
                    "print(count(group=G0.where("
                    "Expression(bad, Value()) != None)))\n"
                ),
            )
        session_row = get_session(db, session.id)
        assert session_row is not None
        assert session_row.state_revision == outcome.state_revision

        recovered = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=outcome.state_revision,
            code="del bad\nprint(1)\n",
        )
        assert recovered.printed_output == "1\n"
        assert "bad" not in load_bindings(db, session.id)


def test_stale_kind_field_binding_survives_dataset_switch(tmp_path: Path) -> None:
    notes_csv = tmp_path / "notes.csv"
    meta_csv = tmp_path / "meta.csv"
    notes_csv.write_text("id,title,body\ne1,Hello,hydrangea\n", encoding="utf-8")
    meta_csv.write_text("id,title\nm1,Meta\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    with db:
        import_csv_dataset(db, "ws", "notes", notes_csv, activate=True)
        import_csv_dataset(db, "ws", "meta", meta_csv, activate=True)
        session = create_session(db, "ws")

        first = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code='f = Field("body", "source")\nprint(f.kind)\n',
        )
        assert first.printed_output == "source\n"

        second = exec_script(
            db,
            session_id=session.id,
            dataset_id="meta",
            expected_revision=first.state_revision,
            code='create_field("body")\nprint(1)\n',
        )
        assert second.printed_output == "1\n"

        third = exec_script(
            db,
            session_id=session.id,
            dataset_id="meta",
            expected_revision=second.state_revision,
            code="print(1)\n",
        )
        assert third.printed_output == "1\n"
        assert "f" in load_bindings(db, session.id)

        recovered = exec_script(
            db,
            session_id=session.id,
            dataset_id="meta",
            expected_revision=third.state_revision,
            code="del f\nprint(1)\n",
        )
        assert recovered.printed_output == "1\n"
        assert "f" not in load_bindings(db, session.id)


def test_stamp_encoded_bindings_stamps_field_and_group_once() -> None:
    field = encode_binding_value(Field("body"))
    group = encode_binding_value(G0)
    stamped = stamp_encoded_bindings(
        {"body": field, "all_rows": group, "n": encode_binding_value(1)},
        "notes",
    )
    decoded_field = decode_binding_value(stamped["body"].value_kind, stamped["body"].value)
    assert decoded_field == Field("body")
    assert decoded_field.bound_dataset_id == "notes"
    decoded_group = decode_binding_value(
        stamped["all_rows"].value_kind, stamped["all_rows"].value
    )
    assert decoded_group.name == "G0"
    assert decoded_group.bound_dataset_id == "notes"
    assert decode_binding_value(stamped["n"].value_kind, stamped["n"].value) == 1
    assert "bound_dataset_id" not in field.value

    already = encode_binding_value(Field("body", bound_dataset_id="notes"))
    kept = stamp_encoded_bindings({"body": already}, "articles")
    decoded_kept = decode_binding_value(kept["body"].value_kind, kept["body"].value)
    assert decoded_kept.bound_dataset_id == "notes"


def test_bound_field_and_group_rejected_on_other_dataset(tmp_path: Path) -> None:
    notes = tmp_path / "notes.csv"
    articles = tmp_path / "articles.csv"
    notes.write_text("id,title,body\ne1,Hello,hydrangea care tips\n", encoding="utf-8")
    articles.write_text("id,title,body\na1,World,climate notes\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", notes, activate=True)
    import_csv_dataset(db, "ws", "articles", articles, activate=True)
    session = create_session(db, "ws")
    with db:
        first = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code=(
                "body = Field('body')\n"
                "matching = G0.where(Expression(body, RegexSearch('hydrangea')) != None)\n"
                "print(count(group=matching))\n"
            ),
        )
        assert first.printed_output == "1\n"
        loaded = load_bindings(db, session.id)
        body = decode_binding_value(loaded["body"].value_kind, loaded["body"].value)
        matching = decode_binding_value(
            loaded["matching"].value_kind, loaded["matching"].value
        )
        assert body.bound_dataset_id == "notes"
        assert matching.bound_dataset_id == "notes"

        with pytest.raises(QuailScopeError, match="no join"):
            exec_script(
                db,
                session_id=session.id,
                dataset_id="articles",
                expected_revision=first.state_revision,
                code="print(retrieve(unit=Unit('values', body), limit=5))\n",
            )
        with pytest.raises(QuailScopeError, match="no join"):
            exec_script(
                db,
                session_id=session.id,
                dataset_id="articles",
                expected_revision=first.state_revision,
                code="print(count(group=matching))\n",
            )

        reused = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=first.state_revision,
            code="print(count(group=matching))\n",
        )
        assert reused.printed_output == "1\n"


def test_unknown_analysis_field_after_version_evolve_hints_retag(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("id,title,body\ne1,Hello,hydrangea\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    with db:
        first = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code=(
                "topic = create_field('topic')\n"
                "tag(retrieve(limit=1), topic, 'plants')\n"
                "print('tagged')\n"
            ),
        )
        assert first.printed_output == "tagged\n"

        csv_path.write_text(
            "id,title,body\ne1,Hello,hydrangea\ne2,Other,climate\n",
            encoding="utf-8",
        )
        import_csv_dataset(db, "ws", "notes", csv_path, activate=True)

        with pytest.raises(QuailFieldError, match="Retag on the active version"):
            exec_script(
                db,
                session_id=session.id,
                dataset_id="notes",
                expected_revision=first.state_revision,
                code="print(retrieve(unit=Unit('values', Field('topic')), limit=5))\n",
            )

        with pytest.raises(QuailFieldError, match="Unknown field: content$"):
            exec_script(
                db,
                session_id=session.id,
                dataset_id="notes",
                expected_revision=first.state_revision,
                code="print(retrieve(unit=Unit('values', Field('content')), limit=5))\n",
            )
