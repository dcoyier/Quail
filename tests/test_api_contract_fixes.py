"""Verified api_draft contract holes: shapes, identity, and overlay commit."""

from __future__ import annotations

from pathlib import Path

import pytest

from quail.analysis.engine import QueryEngine
from quail.analysis.errors import QuailSyntaxError
from quail.analysis.exec_host import dispatch_call, exec_script, run_analysis
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import G0, GroupExpr
from quail.analysis.operations import AsNumber, Length
from quail.analysis.planner import plan_count, plan_retrieve
from quail.analysis.unit import entries
from quail.datasets import import_csv_dataset, open_core_db
from quail.session import create_session


def _seed(tmp_path: Path):
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,title,body,year\n"
        "e1,Hello,hydrangea care tips,2020\n"
        "e2,Other,climate notes,2019\n"
        "e3,Empty,,\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    return db, session


def test_empty_expression_is_identity(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            matching = G0.where(Expression(Field("title")) == "Hello")
            rows = dispatch_call(engine, "retrieve", (), {"group": matching, "limit": 10})
            assert [entry.id for entry in rows] == ["e1"]

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_count_rejects_expression_unit() -> None:
    score = Expression(Field("body"), Length())
    with pytest.raises(QuailSyntaxError, match="count unit must be a Unit"):
        plan_count(unit=score)
    with pytest.raises(QuailSyntaxError, match="count unit must be a Unit"):
        plan_count(score)


def test_retrieve_wraps_rankable_expression_and_entry_list(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            length = Expression(Field("body"), Length())
            ranked = dispatch_call(
                engine,
                "retrieve",
                (),
                {"rank": length, "limit": 2},
            )
            assert [entry.id for entry in ranked] == ["e1", "e2"]
            again = dispatch_call(
                engine,
                "retrieve",
                (),
                {"group": ranked, "limit": 10},
            )
            assert [entry.id for entry in again] == ["e1", "e2"]

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_plan_retrieve_wraps_expression_rank() -> None:
    length = Expression(Field("body"), Length())
    plan = plan_retrieve(unit=entries, rank=length, limit=2)
    assert plan.ranking.expression is not None
    assert plan.ranking.expression.operations[-1].kind == "Length"


def test_members_uniquify_by_entry_id(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            rows = dispatch_call(engine, "retrieve", (), {"limit": 50})
            e1 = next(entry for entry in rows if entry.id == "e1")
            group = GroupExpr("entries", members=[e1, e1])
            assert group.members is not None
            assert len(group.members) == 1
            retrieved = dispatch_call(engine, "retrieve", (), {"group": group, "limit": 10})
            assert [entry.id for entry in retrieved] == ["e1"]

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_untag_matches_canonical_json_not_python_equality(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            topic = dispatch_call(engine, "create_field", ("topic",))
            rows = dispatch_call(engine, "retrieve", (), {"limit": 50})
            e1 = next(entry for entry in rows if entry.id == "e1")
            dispatch_call(engine, "tag", ([e1], topic, True))
            dispatch_call(engine, "untag", ([e1], topic, 1))
            assert dispatch_call(engine, "entry_value", (e1, topic)) is True
            dispatch_call(engine, "tag", ([e1], topic, 1))
            dispatch_call(engine, "untag", ([e1], topic, 1.0))
            assert dispatch_call(engine, "entry_value", (e1, topic)) == 1
            dispatch_call(engine, "untag", ([e1], topic, 1))
            assert dispatch_call(engine, "entry_value", (e1, topic)) is None

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_ranking_missing_times_zero_is_zero(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            maybe = dispatch_call(engine, "create_field", ("maybe",))
            rows = dispatch_call(engine, "retrieve", (), {"limit": 50})
            e1 = next(entry for entry in rows if entry.id == "e1")
            dispatch_call(engine, "tag", ([e1], maybe, 5))
            rank = Expression(Field("maybe"), AsNumber()) * 0 + Expression(
                Field("body"), Length()
            )
            ordered = dispatch_call(engine, "retrieve", (), {"rank": rank, "limit": 3})
            assert [entry.id for entry in ordered] == ["e1", "e2", "e3"]

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_unhashable_field_kind_is_syntax_error() -> None:
    with pytest.raises(QuailSyntaxError, match="kind"):
        Field("body", [])  # type: ignore[arg-type]


def test_csv_year_is_string_until_asnumber(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            year = Field("year")
            as_int = dispatch_call(
                engine,
                "count",
                (),
                {"group": G0.where(Expression(year) == 2020)},
            )
            as_text = dispatch_call(
                engine,
                "count",
                (),
                {"group": G0.where(Expression(year) == "2020")},
            )
            as_number = dispatch_call(
                engine,
                "count",
                (),
                {"group": G0.where(Expression(year, AsNumber()) == 2020)},
            )
            assert as_int == 0
            assert as_text == 1
            assert as_number == 1

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_zip_is_injected(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        outcome = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code="""
rows = retrieve(limit=2)
for index, row in zip(range(2), rows):
    print(index, row.id)
""",
        )
        assert "0 e1" in outcome.printed_output
        assert "1 e2" in outcome.printed_output
