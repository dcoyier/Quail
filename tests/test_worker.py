"""Worker subprocess exec_script coverage for Start Here recipes."""

from __future__ import annotations

from pathlib import Path

import pytest

from quail.analysis.exec_host import exec_script
from quail.analysis.errors import QuailRuntimeError
from quail.analysis.worker.sandbox import validate_quail_code
from quail.datasets import import_csv_dataset, open_core_db
from quail.session import (
    analysis_values,
    catalog_fields,
    create_session,
    get_session,
    resolve_scope,
)


def _seed(tmp_path: Path):
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,title,body\ne1,Hello,hydrangea care tips\ne2,Other,climate notes\ne3,Empty,\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    return db, session


def test_worker_retrieve_fields_and_entry_value(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        code = """
for field in retrieve(unit=fields, group=G1, limit=50):
    print(field.name, field.kind)
samples = retrieve(limit=1)
sample = samples[0]
for field in sample.fields():
    print(field.name, repr(sample.value(field)))
"""
        outcome = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code=code,
        )
        assert "title source" in outcome.printed_output
        assert "body source" in outcome.printed_output
        assert "title 'Hello'" in outcome.printed_output
        # Loop/locals (field, samples, sample) persist as session bindings.
        assert outcome.state_revision == 1


def test_worker_regex_filter(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        code = """
content = Field("body")
mentions = Expression(content, RegexSearch("hydrangea", flags=0)) != None
matching = G0.where(mentions)
print("matches", count(group=matching))
for entry in retrieve(group=matching, limit=10):
    print(entry.id)
"""
        outcome = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code=code,
        )
        assert "matches 1" in outcome.printed_output
        assert "e1" in outcome.printed_output


def test_worker_create_field_and_tag(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        code = """
selected = G0.where(
    Expression(Field("body"), RegexSearch("climate", flags=0)) != None
)
topic = create_field("topic")
tag(selected, topic, "climate")
print(count(group=G0.where(Expression(topic, Value()) == "climate")))
"""
        outcome = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code=code,
        )
        assert outcome.printed_output.strip() == "1"
        assert outcome.state_revision == 1
        scope = resolve_scope(db, session.id, "notes")
        assert ("topic", "analysis") in [
            (field.name, field.kind) for field in catalog_fields(db, scope)
        ]
        assert analysis_values(db, scope, "topic") == [None, "climate", None]


def test_worker_failure_does_not_commit(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        code = """
create_field("topic")
raise QuailRuntimeError("boom")
"""
        # raise is rejected by sandbox — use a failing host call instead
        code = """
create_field("topic")
print(1 / 0)
"""
        with pytest.raises(QuailRuntimeError):
            exec_script(
                db,
                session_id=session.id,
                dataset_id="notes",
                expected_revision=0,
                code=code,
            )
        session = get_session(db, session.id)
        assert session is not None and session.state_revision == 0
        scope = resolve_scope(db, session.id, "notes")
        assert all(field.kind == "source" for field in catalog_fields(db, scope))


def test_validate_rejects_import(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(Exception, match="Unsupported construct|Import"):
        validate_quail_code("import os\nprint(1)\n")
