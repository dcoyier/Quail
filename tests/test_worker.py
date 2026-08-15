"""Worker subprocess exec_script coverage for Start Here recipes."""

from __future__ import annotations

from pathlib import Path

import pytest

from quail.analysis.errors import (
    QuailRuntimeError,
    QuailServerBusyError,
    rehydrate_quail_error,
)
from quail.analysis.exec_host import exec_script
from quail.analysis.worker.client import run_worker_script
from quail.analysis.worker.protocol import ApiCall
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


def test_rehydrate_quail_error_preserves_repair_hint() -> None:
    error = rehydrate_quail_error(
        "QuailRuntimeError",
        "Lexical search is not configured",
        "Set core.search_database, then retry.",
    )
    assert isinstance(error, QuailRuntimeError)
    assert error.repair_hint == "Set core.search_database, then retry."

    busy = rehydrate_quail_error(
        "QuailServerBusyError",
        "too many execs",
        "Retry after another quail_exec finishes.",
    )
    assert isinstance(busy, QuailServerBusyError)
    assert busy.repair_hint == "Retry after another quail_exec finishes."

    bare = rehydrate_quail_error("QuailRuntimeError", "boom")
    assert isinstance(bare, QuailRuntimeError)
    assert bare.repair_hint is None

    rss = rehydrate_quail_error(
        "QuailRssLimitError",
        "quail_exec exceeded its 256 MiB worker RSS limit",
        "Reduce materialized results.",
    )
    from quail.analysis.errors import QuailRssLimitError

    assert isinstance(rss, QuailRssLimitError)
    assert rss.repair_hint == "Reduce materialized results."


def test_worker_rpc_preserves_runtime_repair_hint() -> None:
    hint = "Set core.search_database, re-run quail, then retry the whole exec."

    def on_api_call(call: ApiCall) -> object:
        del call
        raise QuailRuntimeError("Lexical search is not configured", repair_hint=hint)

    with pytest.raises(QuailRuntimeError, match="not configured") as raised:
        run_worker_script("print(count())", on_api_call=on_api_call)
    assert raised.value.repair_hint == hint


def test_exec_script_preserves_lexical_repair_hint(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        with pytest.raises(QuailRuntimeError, match="Lexical search is not configured") as raised:
            exec_script(
                db,
                session_id=session.id,
                dataset_id="notes",
                expected_revision=0,
                code=(
                    'print(count(group=G0.where('
                    'Expression(Field("body"), Lexical("x")) > 0)))\n'
                ),
            )
        assert raised.value.repair_hint is not None
        assert "search_database" in raised.value.repair_hint
        assert "quail process" in raised.value.repair_hint
