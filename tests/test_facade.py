"""Facade AST: symbolic language without execution."""

from __future__ import annotations

import pytest

from quail.analysis import (
    AsNumber,
    AsText,
    Expression,
    Field,
    G0,
    Predicate,
    QuailSyntaxError,
    Value,
    entries,
    make_entry,
)


def test_expression_comparison_builds_predicate() -> None:
    predicate = Expression(Field("theme"), Value()) == "trust"
    assert isinstance(predicate, Predicate)
    assert predicate.operator == "=="
    assert predicate.to_record()["right"] == "trust"


def test_g0_where_composes_filter_group() -> None:
    group = G0.where(Expression(Field("theme"), Value()) == "trust")
    assert group.operator == "and"
    assert group.left is G0
    assert group.right is not None
    assert group.right.predicate is not None
    assert group.right.predicate.operator == "=="


def test_predicate_boolean_ops() -> None:
    left = Expression(Field("a"), Value()) == "x"
    right = Expression(Field("b"), Value(), AsNumber()) > 0
    combined = left & right
    assert combined.operator == "and"
    assert (~left).operator == "not"


def test_pipeline_must_start_with_value() -> None:
    with pytest.raises(QuailSyntaxError, match="Value"):
        Expression(Field("a"), AsText())


def test_extend_expression_pipeline() -> None:
    expr = Expression(Expression(Field("n"), Value()), AsNumber())
    assert [op.kind for op in expr.operations] == ["Value", "AsNumber"]


def test_field_direct_compare_is_rejected() -> None:
    with pytest.raises(QuailSyntaxError, match="Expression"):
        _ = Field("theme") == "trust"


def test_predicate_not_usable_as_python_bool() -> None:
    predicate = Expression(Field("theme"), Value()) == "trust"
    with pytest.raises(QuailSyntaxError, match="G0.where"):
        bool(predicate)


def test_entries_unit_and_entry_handle() -> None:
    assert entries.scope == "entries"
    entry = make_entry("e1")
    assert entry.entry_id == "e1"
    with pytest.raises(QuailSyntaxError, match="Quail"):
        from quail.analysis.facade import Entry

        Entry("e1")
