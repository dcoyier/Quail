"""Lexical query parse, Turso FTS scoring, and QueryEngine wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from quail.analysis.engine import QueryEngine
from quail.analysis.errors import QuailRuntimeError
from quail.analysis.exec_host import dispatch_call, exec_script, run_analysis
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import G0
from quail.analysis.operations import Lexical
from quail.analysis.ranking import Ranking
from quail.datasets import import_csv_dataset, open_core_db
from quail.search import LexicalService, open_search_db
from quail.search.lexical.query import (
    BooleanExpression,
    Leaf,
    LeafKind,
    OrExpression,
    compile_query,
    parse_queries,
)
from quail.session import create_session


def test_parse_term_phrase_and_not_prefix_and_implicit_or() -> None:
    (term,) = parse_queries(("rose",))
    assert isinstance(term, Leaf) and term.kind is LeafKind.TERM

    (phrase,) = parse_queries(('"blue rose"',))
    assert isinstance(phrase, Leaf) and phrase.kind is LeafKind.PHRASE
    assert phrase.terms == ("blue", "rose")

    (anded,) = parse_queries(("rose AND soil",))
    assert isinstance(anded, BooleanExpression)
    assert len(anded.required) == 2 and not anded.excluded

    (excluded,) = parse_queries(("rose NOT soil",))
    assert isinstance(excluded, BooleanExpression)
    assert len(excluded.required) == 1 and len(excluded.excluded) == 1

    (prefix,) = parse_queries(("ros*",))
    assert isinstance(prefix, Leaf) and prefix.kind is LeafKind.PREFIX

    (implicit_or,) = parse_queries(("rose soil",))
    assert isinstance(implicit_or, OrExpression)
    assert len(implicit_or.expressions) == 2

    compiled = compile_query(term, {})
    assert compiled == '"rose"'


def test_parse_rejects_pure_negative_and_explicit_or() -> None:
    with pytest.raises(QuailRuntimeError, match="Pure-negative"):
        parse_queries(("NOT rose",))
    with pytest.raises(QuailRuntimeError, match="Explicit OR"):
        parse_queries(("rose OR soil",))
    with pytest.raises(QuailRuntimeError, match="Bare wildcard"):
        parse_queries(("*",))


def test_lexical_score_reuses_warm_segments_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quail.config.models import SearchWarmConfig
    from quail.search.lexical import corpus as corpus_mod
    from quail.search.warm import warm_dataset

    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,hydrangea care tips\ne2,climate policy notes\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    imported = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    search = open_search_db(tmp_path / "search.turso")
    warm_dataset(
        db,
        search,
        workspace_id="ws",
        dataset_id="notes",
        version_id=imported.version_id,
        profile=None,
        warm=SearchWarmConfig(),
        embedder_factory=lambda _profile: (_ for _ in ()).throw(RuntimeError("no embed")),
    )

    ensure_calls: list[int] = []
    original_ensure = corpus_mod.ensure_entry_segments

    def _counting_ensure(*args: object, **kwargs: object) -> dict[str, int]:
        ensure_calls.append(1)
        return original_ensure(*args, **kwargs)

    monkeypatch.setattr(corpus_mod, "ensure_entry_segments", _counting_ensure)
    monkeypatch.setattr(
        "quail.search.lexical.service.service.ensure_entry_segments",
        _counting_ensure,
    )

    service = LexicalService(search=search)
    corpus = {
        "e1": "hydrangea care tips",
        "e2": "climate policy notes",
    }
    first = service.lexical_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id=imported.version_id,
        corpus_by_entry=corpus,
        query_record={"kind": "LiteralText", "text": "hydrangea"},
        input_aggregation=None,
        target_aggregation=None,
    )
    second = service.lexical_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id=imported.version_id,
        corpus_by_entry=corpus,
        query_record={"kind": "LiteralText", "text": "hydrangea"},
        input_aggregation=None,
        target_aggregation=None,
    )
    assert first["e1"] > 0
    assert first["e2"] == 0.0
    assert second == first
    assert ensure_calls == []
    search.close()


def test_lexical_match_and_miss(tmp_path: Path) -> None:
    search = open_search_db(tmp_path / "search.turso")
    service = LexicalService(search=search)
    scores = service.lexical_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus_by_entry={
            "e1": "hydrangea care tips",
            "e2": "climate policy notes",
        },
        query_record={"kind": "LiteralText", "text": "hydrangea"},
        input_aggregation=None,
        target_aggregation=None,
    )
    assert scores["e1"] > 0
    assert scores["e2"] == 0.0
    search.close()


def test_lexical_phrase_and_not(tmp_path: Path) -> None:
    search = open_search_db(tmp_path / "search.turso")
    service = LexicalService(search=search)
    scores = service.lexical_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus_by_entry={
            "e1": "blue rose garden",
            "e2": "rose soil mix",
            "e3": "tulip bed",
        },
        query_record={"kind": "LiteralText", "text": "rose NOT soil"},
        input_aggregation=None,
        target_aggregation=None,
    )
    assert scores["e1"] > 0
    assert scores["e2"] == 0.0
    assert scores["e3"] == 0.0

    phrase_scores = service.lexical_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus_by_entry={
            "e1": "blue rose garden",
            "e2": "rose blue garden",
        },
        query_record={"kind": "LiteralText", "text": '"blue rose"'},
        input_aggregation=None,
        target_aggregation=None,
    )
    assert phrase_scores["e1"] > 0
    assert phrase_scores["e2"] == 0.0
    search.close()


def test_lexical_prefix(tmp_path: Path) -> None:
    search = open_search_db(tmp_path / "search.turso")
    service = LexicalService(search=search)
    scores = service.lexical_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus_by_entry={
            "e1": "hydrangea blooms",
            "e2": "climate notes",
        },
        query_record={"kind": "LiteralText", "text": "hydran*"},
        input_aggregation=None,
        target_aggregation=None,
    )
    assert scores["e1"] > 0
    assert scores["e2"] == 0.0
    search.close()


def test_lexical_aggregations(tmp_path: Path) -> None:
    search = open_search_db(tmp_path / "search.turso")
    service = LexicalService(search=search)
    corpus = {"e1": ["rose garden", "soil tips", "unrelated"]}
    total = service.lexical_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus_by_entry=corpus,
        query_record={"kind": "LiteralText", "text": "rose"},
        input_aggregation="total",
        target_aggregation=None,
    )
    average = service.lexical_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus_by_entry=corpus,
        query_record={"kind": "LiteralText", "text": "rose"},
        input_aggregation="avg",
        target_aggregation=None,
    )
    assert total["e1"] > 0
    assert average["e1"] == pytest.approx(total["e1"] / 3)

    multi_total = service.lexical_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus_by_entry={"e1": "rose garden"},
        query_record={"kind": "LiteralTextList", "texts": ["rose", "garden"]},
        input_aggregation="total",
        target_aggregation="total",
    )
    multi_avg = service.lexical_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus_by_entry={"e1": "rose garden"},
        query_record={"kind": "LiteralTextList", "texts": ["rose", "garden"]},
        input_aggregation="total",
        target_aggregation="avg",
    )
    assert multi_total["e1"] > 0
    assert multi_avg["e1"] == pytest.approx(multi_total["e1"] / 2)
    search.close()


def test_lexical_service_rejects_unresolved_entry_group(tmp_path: Path) -> None:
    search = open_search_db(tmp_path / "search.turso")
    service = LexicalService(search=search)
    with pytest.raises(QuailRuntimeError, match="must be resolved by QueryEngine"):
        service.lexical_score(
            workspace_id="ws",
            dataset_id="notes",
            version_id="v1",
            corpus="hello",
            query_record={"kind": "EntryGroup", "group": {}},
            input_aggregation=None,
            target_aggregation=None,
        )
    search.close()


def test_engine_lexical_filter_without_search_db(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("id,body\ne1,hydrangea care\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    with db:
        score = Expression(Field("body"), Lexical("hydrangea"))
        matching = G0.where(score > 0)

        def driver(engine: QueryEngine, _prints) -> None:
            with pytest.raises(QuailRuntimeError, match="Lexical search is not configured"):
                dispatch_call(engine, "count", (), {"group": matching})

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
        )


def test_engine_lexical_filter_and_ranking(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,hydrangea care tips\ne2,climate policy notes\ne3,garden hydrangea bloom\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    search = open_search_db(tmp_path / "search.turso")
    lexical = LexicalService(search=search)
    with db:
        score = Expression(Field("body"), Lexical("hydrangea"))
        matching = G0.where(score > 0)

        def driver(engine: QueryEngine, _prints) -> None:
            total = dispatch_call(engine, "count", (), {"group": matching})
            assert total == 2
            ranked = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "group": G0,
                    "rank": Ranking(expression=score),
                    "order": "top",
                    "limit": 2,
                },
            )
            assert {entry.id for entry in ranked} == {"e1", "e3"}

            bottom = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "group": matching,
                    "rank": Ranking(expression=score),
                    "order": "bottom",
                    "limit": 1,
                },
            )
            assert len(bottom) == 1
            assert bottom[0].id in {"e1", "e3"}

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            lexical=lexical,
        )
    search.close()


def test_exec_script_lexical_path(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,hydrangea care\ne2,climate notes\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    search = open_search_db(tmp_path / "search.turso")
    lexical = LexicalService(search=search)
    with db:
        outcome = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code=(
                "score = Expression(Field('body'), Lexical('hydrangea'))\n"
                "rows = retrieve(group=G0.where(score > 0), limit=10)\n"
                "ids = []\n"
                "for entry in rows:\n"
                "    ids = ids + [entry.id]\n"
                "print(ids)\n"
            ),
            lexical=lexical,
        )
        assert "e1" in outcome.printed_output
        assert "e2" not in outcome.printed_output
    search.close()


def test_lexical_entry_group_and_entry_list_targets(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,hydrangea care tips\ne2,climate policy notes\ne3,garden hydrangea bloom\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    search = open_search_db(tmp_path / "search.turso")
    lexical = LexicalService(search=search)
    with db:
        targets = G0.where(Expression(Field("body"), Lexical("climate")) > 0)
        score = Expression(Field("body"), Lexical(targets))

        def driver(engine: QueryEngine, _prints) -> None:
            # Query targets are climate entries; corpus is body. e2 should match itself.
            rows = dispatch_call(
                engine,
                "retrieve",
                (),
                {"group": G0.where(score > 0), "limit": 10},
            )
            assert [entry.id for entry in rows] == ["e2"]

            entries = dispatch_call(engine, "retrieve", (), {"group": G0, "limit": 10})
            by_id = {entry.id: entry for entry in entries}
            list_score = Expression(Field("body"), Lexical([by_id["e1"], by_id["e3"]]))
            ranked = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "group": G0,
                    "rank": Ranking(expression=list_score),
                    "order": "top",
                    "limit": 2,
                },
            )
            assert {entry.id for entry in ranked} == {"e1", "e3"}

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            lexical=lexical,
        )
    search.close()


def test_lexical_entry_target_quotes_and_operator_text(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,rose AND soil\ne2,rose garden\ne3,tulip bed\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    search = open_search_db(tmp_path / "search.turso")
    lexical = LexicalService(search=search)
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            entries = dispatch_call(engine, "retrieve", (), {"group": G0, "limit": 10})
            by_id = {entry.id: entry for entry in entries}
            # Entry-derived target must not treat AND as FTS syntax.
            score = Expression(Field("body"), Lexical([by_id["e1"]]))
            rows = dispatch_call(
                engine,
                "retrieve",
                (),
                {"group": G0.where(score > 0), "limit": 10},
            )
            ids = {entry.id for entry in rows}
            assert "e1" in ids
            assert "e2" in ids
            assert "e3" not in ids

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            lexical=lexical,
        )
    search.close()


def test_lexical_empty_entry_targets_error(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("id,body\ne1,\ne2,\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    search = open_search_db(tmp_path / "search.turso")
    lexical = LexicalService(search=search)
    with db:
        score = Expression(Field("body"), Lexical(G0))

        def driver(engine: QueryEngine, _prints) -> None:
            with pytest.raises(QuailRuntimeError, match="no non-empty target text"):
                dispatch_call(
                    engine,
                    "retrieve",
                    (),
                    {"group": G0.where(score > 0), "limit": 10},
                )

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            lexical=lexical,
        )
    search.close()
