"""Symbolic facade AST covering docs/api.md Start Here recipes."""

from __future__ import annotations

import pytest

from quail.analysis import (
    AsNumber,
    AsText,
    Expression,
    Field,
    G0,
    G1,
    Length,
    Lexical,
    Predicate,
    QuailRuntimeError,
    QuailScopeError,
    QuailSyntaxError,
    Ranking,
    RegexFindAll,
    RegexSearch,
    Semantic,
    Unit,
    Value,
    api_namespace,
    entries,
    fields,
    make_entry,
)


def test_filter_recipe_with_regex() -> None:
    content = Field("content")
    mentions = Expression(content, RegexSearch("hydrangea", flags=0)) != None  # noqa: E711
    matching = G0.where(mentions)
    assert isinstance(mentions, Predicate)
    assert matching.operator == "and"
    assert matching.right is not None
    assert matching.right.predicate is not None


def test_rank_recipe_with_lexical() -> None:
    score = Expression(Field("content"), Lexical("hydrangea care"))
    matching = G0.where(score > 0)
    rank = Ranking(expression=score)
    assert rank.expression is score
    assert matching.right is not None


def test_lexical_not_tied_to_ranking() -> None:
    score = Expression(Field("content"), Lexical("climate"))
    # Usable as predicate without Ranking
    group = G0.where(score > 0)
    assert group.operator == "and"


def test_combine_regex_count_and_lexical_via_predicates() -> None:
    hit_count = Expression(Field("content"), RegexFindAll(r"climate\w*"), Length())
    lex = Expression(Field("content"), Lexical("climate policy"))
    group = G0.where((hit_count >= 1) & (lex > 0))
    assert group.right is not None
    assert group.right.predicate is not None
    assert group.right.predicate.operator == "and"


def test_ranking_addition_and_weight() -> None:
    a = Expression(Field("content"), Length())
    b = Expression(Field("content"), Lexical("x"))
    rank = Ranking(expression=a) + (Ranking(expression=b) * 2.0)
    assert rank.operator == "+"
    assert isinstance(rank.right, Ranking)
    assert rank.right.operator == "*"


def test_pipeline_and_units() -> None:
    expr = Expression(Field("n"), Value(), AsNumber())
    assert [op.kind for op in expr.operations] == ["Value", "AsNumber"]
    assert entries.scope == "entries"
    assert fields.scope == "fields"
    assert Unit("values", Field("topic")).field is not None
    assert G1.name == "G1"


def test_lexical_must_end_pipeline() -> None:
    with pytest.raises(QuailSyntaxError, match="must end"):
        Expression(Field("content"), Lexical("a"), AsText())


def test_search_query_rejects_field_scoped_group() -> None:
    with pytest.raises(QuailScopeError, match="entry-scoped"):
        Lexical(G1)
    with pytest.raises(QuailScopeError, match="entry-scoped"):
        Semantic(G1)


def test_semantic_query_and_namespace_inventory() -> None:
    op = Semantic("hello", input_aggregation="avg")
    assert op.kind == "Semantic"
    assert op.input_aggregation == "avg"
    ns = api_namespace()
    assert "G0" in ns and "re" in ns and "Lexical" in ns
    assert "retrieve" not in ns


def test_entry_handle_and_field_compare_rejected() -> None:
    entry = make_entry("e1", dataset_id="d", dataset_version_id="v")
    assert entry.id == "e1"
    with pytest.raises(QuailRuntimeError, match="entry.value"):
        entry.value(Field("content"))
    with pytest.raises(QuailSyntaxError, match="Expression"):
        _ = Field("theme") == "trust"


def test_group_not_iterable() -> None:
    with pytest.raises(QuailSyntaxError, match="retrieve"):
        iter(G0)
