"""Host planner + QueryEngine Start Here coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from quail.analysis.engine import QueryEngine
from quail.analysis.exec_host import dispatch_call, run_analysis
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import G0, G1
from quail.analysis.operations import Lexical, RegexSearch, Value
from quail.analysis.unit import fields
from quail.datasets import import_csv_dataset, open_core_db
from quail.session import analysis_values, catalog_fields, create_session, get_session


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


def test_retrieve_fields_and_entry_value(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, prints) -> None:
            catalog = dispatch_call(
                engine, "retrieve", (), {"unit": fields, "group": G1, "limit": 50}
            )
            assert [field.name for field in catalog] == ["title", "body"]
            samples = dispatch_call(engine, "retrieve", (), {"limit": 1})
            sample = samples[0]
            present = dispatch_call(engine, "entry_fields", (sample,))
            assert {field.name for field in present} == {"title", "body"}
            title = dispatch_call(engine, "entry_value", (sample, Field("title")))
            assert title == "Hello"
            prints.write(title)

        outcome = run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )
        assert outcome.printed_output == "Hello\n"
        assert outcome.state_revision == 0


def test_regex_filter_count_and_retrieve(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        content = Field("body")
        mentions = Expression(content, RegexSearch("hydrangea", flags=0)) != None  # noqa: E711
        matching = G0.where(mentions)

        def driver(engine: QueryEngine, _prints) -> None:
            total = dispatch_call(engine, "count", (), {"group": matching})
            assert total == 1
            rows = dispatch_call(engine, "retrieve", (), {"group": matching, "limit": 10})
            assert [entry.id for entry in rows] == ["e1"]

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_create_field_tag_mid_run_and_commit(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            topic = dispatch_call(engine, "create_field", ("topic",))
            assert topic.name == "topic"
            selected = G0.where(
                Expression(Field("body"), RegexSearch("climate", flags=0)) != None  # noqa: E711
            )
            dispatch_call(engine, "tag", (selected, topic, "climate"))
            tagged = G0.where(Expression(topic, Value()) == "climate")
            assert dispatch_call(engine, "count", (), {"group": tagged}) == 1

        outcome = run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )
        assert outcome.state_revision == 1
        session = get_session(db, session.id)
        assert session is not None and session.state_revision == 1
        from quail.session import resolve_scope

        scope = resolve_scope(db, session.id, "notes")
        kinds = [(field.name, field.kind) for field in catalog_fields(db, scope)]
        assert ("topic", "analysis") in kinds
        assert analysis_values(db, scope, "topic") == [None, "climate", None]


def test_failed_driver_does_not_commit(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            dispatch_call(engine, "create_field", ("topic",))
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            run_analysis(
                db,
                session_id=session.id,
                dataset_id="notes",
                expected_revision=0,
                driver=driver,
            )
        session = get_session(db, session.id)
        assert session is not None and session.state_revision == 0
        from quail.session import resolve_scope

        scope = resolve_scope(db, session.id, "notes")
        assert all(field.kind == "source" for field in catalog_fields(db, scope))


def test_lexical_not_wired(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        score = Expression(Field("body"), Lexical("hydrangea care"))
        matching = G0.where(score > 0)

        def driver(engine: QueryEngine, _prints) -> None:
            with pytest.raises(Exception, match="not wired"):
                dispatch_call(engine, "count", (), {"group": matching})

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )
