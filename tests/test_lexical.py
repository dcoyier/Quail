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
from quail.analysis.operations import Lexical, Value
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


def test_parse_hyphenated_atom_becomes_any_leaf() -> None:
    (leaf,) = parse_queries(("hydrangea-care",))
    assert isinstance(leaf, Leaf)
    assert leaf.kind is LeafKind.ANY
    assert leaf.terms == ("hydrangea", "care")
    assert compile_query(leaf, {}) == '("hydrangea" OR "care")'


def test_parse_hyphenated_atom_keeps_and_binding() -> None:
    (expression,) = parse_queries(("foo-bar AND baz",))
    assert isinstance(expression, BooleanExpression)
    assert len(expression.required) == 2
    left, right = expression.required
    assert left.kind is LeafKind.ANY and left.terms == ("foo", "bar")
    assert right.kind is LeafKind.TERM and right.terms == ("baz",)
    assert compile_query(expression, {}) == '(("foo" OR "bar") AND "baz")'


def test_parse_hyphenated_atom_keeps_not_binding() -> None:
    (expression,) = parse_queries(("alpha NOT foo-bar",))
    assert isinstance(expression, BooleanExpression)
    assert expression.required == (Leaf(LeafKind.TERM, ("alpha",)),)
    assert expression.excluded == (Leaf(LeafKind.ANY, ("foo", "bar")),)
    assert compile_query(expression, {}) == '("alpha" NOT ("foo" OR "bar"))'


def test_parse_prefix_still_rejects_punctuated_atoms() -> None:
    with pytest.raises(QuailRuntimeError, match="prefixes require a single term"):
        parse_queries(("foo-bar*",))
    with pytest.raises(QuailRuntimeError, match="removed by the current lexical tokenizer"):
        parse_queries(("---",))


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
        source_field="body",
    )
    second = service.lexical_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id=imported.version_id,
        corpus_by_entry=corpus,
        query_record={"kind": "LiteralText", "text": "hydrangea"},
        input_aggregation=None,
        target_aggregation=None,
        source_field="body",
    )
    assert first["e1"] > 0
    assert first["e2"] == 0.0
    assert second == first
    assert ensure_calls == []
    search.close()


def test_engine_uses_warmed_source_lexical_without_dynamic_corpus_or_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quail.config.models import SearchWarmConfig
    from quail.search.warm import warm_dataset

    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,climate policy notes\ne2,hydrangea care tips\n",
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
    service = LexicalService(search=search)
    session = create_session(db, "ws")

    def _reject_dynamic(*_args: object, **_kwargs: object) -> dict[str, float]:
        raise AssertionError("warmed source Lexical must not materialize dynamic corpus")

    def _reject_counts(*_args: object, **_kwargs: object) -> dict[str, int]:
        raise AssertionError("total aggregation must not count every source segment")

    monkeypatch.setattr(LexicalService, "lexical_scores_for_entries", _reject_dynamic)
    monkeypatch.setattr(
        "quail.search.lexical.service.service.load_entry_segment_counts",
        _reject_counts,
    )

    def driver(engine: QueryEngine, _prints) -> None:
        score = Expression(Field("body"), Value(), Lexical("hydrangea"))
        ranked = dispatch_call(
            engine,
            "retrieve",
            (),
            {
                "group": G0,
                "rank": Ranking(expression=score),
                "limit": 2,
            },
        )
        assert [entry.id for entry in ranked] == ["e2", "e1"]
        assert dispatch_call(engine, "count", (), {"group": G0.where(score > 0)}) == 1

    try:
        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            lexical=service,
        )
    finally:
        search.close()
        db.close()


def test_ensure_entry_segments_commits_in_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quail.search.lexical.corpus import corpus as corpus_mod
    from quail.search.lexical.corpus import ensure_entry_segments, resolve_corpus

    monkeypatch.setattr(corpus_mod, "_ENTRY_COMMIT_BATCH_SIZE", 2)
    search = open_search_db(tmp_path / "search.turso")
    corpus = resolve_corpus(
        search,
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        field_name="body",
    )
    # resolve_corpus must not create FTS up front (FTS-last warm order).
    fts_name = f"{corpus.doc_table}_fts"
    assert (
        search.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (fts_name,),
        ).fetchone()
        is None
    )
    commits: list[int] = []
    original_commit = search.connection.commit

    def _counting_commit() -> None:
        commits.append(1)
        original_commit()

    monkeypatch.setattr(search.connection, "commit", _counting_commit)
    counts = ensure_entry_segments(
        search,
        corpus,
        entry_segments={
            "e1": ["alpha"],
            "e2": ["beta"],
            "e3": ["gamma"],
            "e4": ["delta"],
            "e5": ["epsilon"],
        },
    )
    assert counts == {"e1": 1, "e2": 1, "e3": 1, "e4": 1, "e5": 1}
    # 5 entries / batch size 2 → 3 write commits, then one FTS-create commit.
    assert len(commits) == 4
    assert (
        search.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (fts_name,),
        ).fetchone()
        is not None
    )
    loaded = corpus_mod.load_entry_segment_counts(
        search, corpus, entry_ids=["e1", "e2", "e3", "e4", "e5"]
    )
    assert loaded == counts
    search.close()


def test_warm_entry_segments_builds_fts_after_rows(tmp_path: Path) -> None:
    from quail.search.lexical.corpus import resolve_corpus, warm_entry_segments

    search = open_search_db(tmp_path / "search.turso")
    corpus = resolve_corpus(
        search,
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        field_name="body",
    )
    fts_name = f"{corpus.doc_table}_fts"
    create_order: list[str] = []
    original_execute = search.connection.execute

    def _tracking_execute(sql: object, params: object = ()) -> object:
        text = str(sql)
        if "CREATE INDEX" in text and "USING fts" in text:
            create_order.append("fts")
        if "INSERT INTO" in text and corpus.doc_table in text:
            create_order.append("insert")
        return original_execute(sql, params)

    search.connection.execute = _tracking_execute  # type: ignore[method-assign]
    warm_entry_segments(
        search,
        corpus,
        entry_segments={"e1": ["hydrangea care"], "e2": ["climate notes"]},
    )
    assert "insert" in create_order
    assert "fts" in create_order
    assert create_order.index("insert") < create_order.index("fts")
    assert (
        search.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (fts_name,),
        ).fetchone()
        is not None
    )
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


class _CountingLexical(LexicalService):
    """LexicalService wrapper that counts batch score calls."""

    batch_calls: int = 0

    def lexical_scores_for_entries(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        version_id: str,
        corpus_by_entry: object,
        query_record: dict[str, object],
        input_aggregation: str | None,
        target_aggregation: str | None,
        source_field: str | None = None,
    ) -> dict[str, float]:
        self.batch_calls += 1
        return super().lexical_scores_for_entries(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            corpus_by_entry=corpus_by_entry,  # type: ignore[arg-type]
            query_record=query_record,  # type: ignore[arg-type]
            input_aggregation=input_aggregation,
            target_aggregation=target_aggregation,
            source_field=source_field,
        )


def test_engine_lexical_where_batches_once_per_expression(tmp_path: Path) -> None:
    rows = ["id,body"] + [f"e{i},note {i} hydrangea garden" for i in range(40)]
    rows[2] = "e1,climate policy only"
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    search = open_search_db(tmp_path / "search.turso")
    lexical = _CountingLexical(search)
    with db:
        score = Expression(Field("body"), Lexical("hydrangea"))
        matching = G0.where(score > 0)

        def driver(engine: QueryEngine, _prints) -> None:
            total = dispatch_call(engine, "count", (), {"group": matching})
            assert total == 39
            # One batch for the where predicate — not one call per entry.
            assert lexical.batch_calls == 1

            ranked = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "group": matching,
                    "rank": Ranking(expression=score),
                    "order": "top",
                    "limit": 5,
                },
            )
            assert len(ranked) == 5
            # Same logical Expression across count + retrieve: one batch for the exec.
            assert lexical.batch_calls == 1

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            lexical=lexical,
        )
    search.close()


def test_engine_lexical_batches_survive_rpc_clones(tmp_path: Path) -> None:
    rows = ["id,body"] + [f"e{i},note {i} hydrangea garden" for i in range(20)]
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    search = open_search_db(tmp_path / "search.turso")
    lexical = _CountingLexical(search)
    with db:
        from quail.analysis.worker.protocol import decode_value, encode_value

        score = Expression(Field("body"), Lexical("hydrangea"))
        matching = G0.where(score > 0)
        cloned_group = decode_value(encode_value(matching))
        cloned_rank = decode_value(encode_value(Ranking(expression=score)))
        cloned_score = decode_value(encode_value(score))
        assert cloned_score is not score

        def driver(engine: QueryEngine, _prints) -> None:
            total = dispatch_call(engine, "count", (), {"group": cloned_group})
            assert total == 20
            ranked = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "group": cloned_group,
                    "rank": cloned_rank,
                    "unit": cloned_score,
                    "order": "top",
                    "limit": 5,
                },
            )
            assert len(ranked) == 5
            assert lexical.batch_calls == 1

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            lexical=lexical,
        )
    search.close()


def test_per_field_warm_isolation_and_scratch(tmp_path: Path) -> None:
    from quail.config.models import SearchWarmConfig
    from quail.search.lexical.corpus import lookup_field_corpus, load_entry_segment_counts
    from quail.search.warm import warm_dataset

    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,title,body\ne1,hydrangea,climate\ne2,climate,hydrangea\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    imported = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    search = open_search_db(tmp_path / "search.turso")
    try:
        warm_dataset(
            db,
            search,
            workspace_id="ws",
            dataset_id="notes",
            version_id=imported.version_id,
            profile=None,
            warm=SearchWarmConfig(),
            embedder_factory=lambda _p: (_ for _ in ()).throw(RuntimeError("no")),
        )
        title = lookup_field_corpus(
            search,
            workspace_id="ws",
            dataset_id="notes",
            version_id=imported.version_id,
            field_name="title",
        )
        body = lookup_field_corpus(
            search,
            workspace_id="ws",
            dataset_id="notes",
            version_id=imported.version_id,
            field_name="body",
        )
        assert title is not None and body is not None
        assert title.doc_table != body.doc_table
        assert load_entry_segment_counts(search, title, entry_ids=["e1", "e2"]) == {
            "e1": 1,
            "e2": 1,
        }
        assert load_entry_segment_counts(search, body, entry_ids=["e1", "e2"]) == {
            "e1": 1,
            "e2": 1,
        }

        service = LexicalService(search=search)
        title_scores = service.lexical_scores_for_entries(
            workspace_id="ws",
            dataset_id="notes",
            version_id=imported.version_id,
            corpus_by_entry={"e1": "hydrangea", "e2": "climate"},
            query_record={"kind": "LiteralText", "text": "hydrangea"},
            input_aggregation=None,
            target_aggregation=None,
            source_field="title",
        )
        body_scores = service.lexical_scores_for_entries(
            workspace_id="ws",
            dataset_id="notes",
            version_id=imported.version_id,
            corpus_by_entry={"e1": "climate", "e2": "hydrangea"},
            query_record={"kind": "LiteralText", "text": "hydrangea"},
            input_aggregation=None,
            target_aggregation=None,
            source_field="body",
        )
        assert title_scores["e1"] > 0 and title_scores["e2"] == 0.0
        assert body_scores["e2"] > 0 and body_scores["e1"] == 0.0

        before_title = search.connection.execute(
            f"SELECT COUNT(*) FROM {title.doc_table}"
        ).fetchone()[0]
        scratch_scores = service.lexical_scores_for_entries(
            workspace_id="ws",
            dataset_id="notes",
            version_id=imported.version_id,
            corpus_by_entry={"e1": "hydrangea extra", "e2": "other"},
            query_record={"kind": "LiteralText", "text": "hydrangea"},
            input_aggregation=None,
            target_aggregation=None,
            source_field=None,
        )
        assert scratch_scores["e1"] > 0
        after_title = search.connection.execute(
            f"SELECT COUNT(*) FROM {title.doc_table}"
        ).fetchone()[0]
        assert after_title == before_title
        leftovers = search.connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name LIKE 'quail_lex_scratch_%'
            """
        ).fetchall()
        assert leftovers == []

        again = service.lexical_scores_for_entries(
            workspace_id="ws",
            dataset_id="notes",
            version_id=imported.version_id,
            corpus_by_entry={"e1": "hydrangea", "e2": "climate"},
            query_record={"kind": "LiteralText", "text": "hydrangea"},
            input_aggregation=None,
            target_aggregation=None,
            source_field="title",
        )
        assert again == title_scores
    finally:
        search.close()
        db.close()


def test_slice_derived_corpus_does_not_reuse_warm_field(tmp_path: Path) -> None:
    from quail.analysis.operations import Slice
    from quail.config.models import SearchWarmConfig
    from quail.search.warm import warm_dataset

    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,hydrangea care tips\ne2,climate policy notes\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    imported = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    search = open_search_db(tmp_path / "search.turso")
    try:
        warm_dataset(
            db,
            search,
            workspace_id="ws",
            dataset_id="notes",
            version_id=imported.version_id,
            profile=None,
            warm=SearchWarmConfig(),
            embedder_factory=lambda _p: (_ for _ in ()).throw(RuntimeError("no")),
        )
        session = create_session(db, workspace_id="ws")
        lexical = LexicalService(search=search)
        sliced = Expression(Field("body", kind="source"), Slice(0, 4), Lexical("care"))
        full = Expression(Field("body", kind="source"), Lexical("care"))

        def driver(engine: QueryEngine, _prints: object) -> None:
            assert dispatch_call(engine, "count", (), {"group": G0.where(sliced > 0)}) == 0
            assert dispatch_call(engine, "count", (), {"group": G0.where(full > 0)}) == 1

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            lexical=lexical,
        )
    finally:
        search.close()
        db.close()
