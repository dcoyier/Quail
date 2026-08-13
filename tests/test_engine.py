"""Host planner + QueryEngine Start Here coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from quail.analysis.engine import QueryEngine
from quail.analysis.errors import QuailFieldError, QuailRuntimeError, QuailScopeError, QuailSyntaxError
from quail.analysis.exec_host import dispatch_call, run_analysis
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import G0, G1, GroupExpr
from quail.analysis.operations import AsText, Lexical, RegexSearch, Value
from quail.analysis.planner import plan_create_field
from quail.analysis.unit import Unit, fields
from quail.datasets import import_csv_dataset, open_core_db
from quail.search import LexicalService, open_search_db
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


def test_lexical_requires_search_database(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        score = Expression(Field("body"), Lexical("hydrangea care"))
        matching = G0.where(score > 0)

        def driver(engine: QueryEngine, _prints) -> None:
            with pytest.raises(QuailRuntimeError, match="Lexical search is not configured") as raised:
                dispatch_call(engine, "count", (), {"group": matching})
            assert raised.value.repair_hint is not None
            assert "quail process" in raised.value.repair_hint
            assert "apply" not in raised.value.repair_hint.lower()

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_regex_search_rejects_non_text(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            score = dispatch_call(engine, "create_field", ("score",))
            sample = dispatch_call(engine, "retrieve", (), {"limit": 1})[0]
            dispatch_call(engine, "tag", ([sample], score, 12))
            with pytest.raises(QuailRuntimeError, match="requires text"):
                dispatch_call(
                    engine,
                    "count",
                    (),
                    {
                        "group": G0.where(
                            Expression(score, RegexSearch("1")) != None  # noqa: E711
                        )
                    },
                )
            assert (
                dispatch_call(
                    engine,
                    "count",
                    (),
                    {
                        "group": G0.where(
                            Expression(score, AsText(), RegexSearch("1")) != None  # noqa: E711
                        )
                    },
                )
                == 1
            )

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_field_kind_mismatch_raises(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            with pytest.raises(QuailFieldError, match="registered as source, not analysis"):
                dispatch_call(
                    engine,
                    "count",
                    (),
                    {
                        "group": G0.where(
                            Expression(Field("body", "analysis"), Value()) != None  # noqa: E711
                        )
                    },
                )
            assert (
                dispatch_call(
                    engine,
                    "count",
                    (),
                    {
                        "group": G0.where(
                            Expression(Field("body", "source"), Value()) != None  # noqa: E711
                        )
                    },
                )
                == 2
            )
            topic = dispatch_call(engine, "create_field", (Field("topic"),))
            with pytest.raises(QuailFieldError, match="registered as analysis, not source"):
                dispatch_call(engine, "tag", (G0, Field("topic", "source"), "x"))
            sample = dispatch_call(engine, "retrieve", (), {"limit": 1})[0]
            dispatch_call(engine, "tag", ([sample], topic, "x"))

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_foreign_entry_rejected_across_datasets(tmp_path: Path) -> None:
    from quail.analysis.entry import make_entry
    from quail.session import resolve_scope

    csv_a = tmp_path / "a.csv"
    csv_b = tmp_path / "b.csv"
    csv_a.write_text("id,title,body\ne1,A,alpha\n", encoding="utf-8")
    csv_b.write_text("id,title,body\ne1,B,beta\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    with db:
        import_csv_dataset(db, "ws", "a", csv_a, activate=True)
        import_csv_dataset(db, "ws", "b", csv_b, activate=True)
        session = create_session(db, "ws")
        scope_a = resolve_scope(db, session.id, "a")
        foreign = make_entry(
            "e1",
            dataset_id="a",
            dataset_version_id=scope_a.dataset_version_id,
            dataset="a",
        )

        def use_on_b(engine: QueryEngine, _prints) -> None:
            topic = dispatch_call(engine, "create_field", ("topic",))
            with pytest.raises(QuailScopeError, match="does not belong"):
                dispatch_call(engine, "tag", ([foreign], topic, "leak"))
            with pytest.raises(QuailScopeError, match="does not belong"):
                dispatch_call(engine, "entry_value", (foreign, Field("title")))
            with pytest.raises(QuailScopeError, match="does not belong"):
                dispatch_call(engine, "entry_fields", (foreign,))
            with pytest.raises(QuailScopeError, match="does not belong"):
                dispatch_call(engine, "untag", ([foreign], topic))
            local = dispatch_call(engine, "retrieve", (), {"limit": 1})[0]
            dispatch_call(engine, "tag", ([local], topic, "ok"))
            assert dispatch_call(engine, "entry_value", (local, topic)) == "ok"

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="b",
            expected_revision=0,
            driver=use_on_b,
        )
        scope_b = resolve_scope(db, session.id, "b")
        assert analysis_values(db, scope_b, "topic") == ["ok"]


class _CountingLexical(LexicalService):
    batch_calls: int = 0

    def lexical_scores_for_entries(self, **kwargs):  # type: ignore[no-untyped-def]
        self.batch_calls += 1
        return super().lexical_scores_for_entries(**kwargs)


def test_tag_invalidates_search_score_cache(tmp_path: Path) -> None:
    """Search → tag → identical search must recompute scores in one exec."""

    db, session = _seed(tmp_path)
    search = open_search_db(tmp_path / "search.turso")
    lexical = _CountingLexical(search=search)
    with db:
        label = Field("label", kind="analysis")
        score = Expression(label, Lexical("hydrangea"))
        matching = G0.where(score > 0)

        def driver(engine: QueryEngine, _prints) -> None:
            created = dispatch_call(engine, "create_field", ("label",))
            entries = dispatch_call(engine, "retrieve", (), {"limit": 50})
            e1 = next(entry for entry in entries if entry.id == "e1")
            dispatch_call(engine, "tag", ([e1], created, "hydrangea care tips"))
            assert dispatch_call(engine, "count", (), {"group": matching}) == 1
            assert lexical.batch_calls == 1

            dispatch_call(engine, "untag", ([e1], created))
            dispatch_call(engine, "tag", ([e1], created, "climate notes only"))
            assert dispatch_call(engine, "count", (), {"group": matching}) == 0
            # Cache must clear on mutation so the second Lexical pass re-scores.
            assert lexical.batch_calls == 2

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            lexical=lexical,
        )
    search.close()


def test_plan_create_field_strips_field_name() -> None:
    plan = plan_create_field(Field("  topic  "))
    assert plan.field.name == "topic"
    assert plan.field.kind == "analysis"
    with pytest.raises(QuailSyntaxError, match="non-empty"):
        plan_create_field(Field("   "))


def test_distinct_values_normalize_dict_key_order(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            meta = dispatch_call(engine, "create_field", ("meta",))
            entries = dispatch_call(engine, "retrieve", (), {"limit": 50})
            e1 = next(entry for entry in entries if entry.id == "e1")
            e2 = next(entry for entry in entries if entry.id == "e2")
            dispatch_call(engine, "tag", ([e1], meta, {"a": 1, "b": 2}))
            dispatch_call(engine, "tag", ([e2], meta, {"b": 2, "a": 1}))
            unit = Unit("values", meta)
            values = dispatch_call(engine, "retrieve", (), {"unit": unit, "limit": 50})
            assert len(values) == 1
            assert dispatch_call(engine, "count", (), {"unit": unit}) == 1

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_values_limit_applies_after_full_group_dedupe(tmp_path: Path) -> None:
    """limit must not truncate entries before distinct collection (Garden Gate stress)."""

    csv_path = tmp_path / "authors.csv"
    # Five shared "170", then five unique authors — old bug returned only ["170"] at limit=5.
    rows = ["id,author"] + [f"e{i},170" for i in range(1, 6)]
    rows += [f"e{i},a{i}" for i in range(6, 11)]
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "authors", csv_path, activate=True)
    session = create_session(db, "ws")
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            unit = Unit("values", Field("author"))
            assert dispatch_call(engine, "count", (), {"unit": unit}) == 6
            top = dispatch_call(engine, "retrieve", (), {"unit": unit, "limit": 5, "order": "top"})
            assert top == ["170", "a6", "a7", "a8", "a9"]
            bottom = dispatch_call(
                engine, "retrieve", (), {"unit": unit, "limit": 3, "order": "bottom"}
            )
            assert bottom == ["a8", "a9", "a10"]
            middle = dispatch_call(
                engine, "retrieve", (), {"unit": unit, "limit": 2, "order": "middle"}
            )
            assert middle == ["a7", "a8"]
            empty = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "unit": unit,
                    "group": G0.where(Expression(Field("author"), Value()) == "missing"),
                    "limit": 5,
                },
            )
            assert empty == []

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="authors",
            expected_revision=0,
            driver=driver,
        )


def test_field_group_members_resolve_against_catalog(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            group = GroupExpr(scope="fields", members=[Field("title"), Field("body")])
            rows = dispatch_call(
                engine, "retrieve", (), {"unit": fields, "group": group, "limit": 50}
            )
            assert [(field.name, field.kind) for field in rows] == [
                ("title", "source"),
                ("body", "source"),
            ]
            unknown = GroupExpr(scope="fields", members=[Field("missing")])
            with pytest.raises(QuailFieldError, match="Unknown field"):
                dispatch_call(
                    engine, "retrieve", (), {"unit": fields, "group": unknown, "limit": 50}
                )

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )
