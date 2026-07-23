"""Injected quail_exec namespace callables (stubs until engine/worker land)."""

from __future__ import annotations

from typing import Any

from quail.analysis.errors import QuailRuntimeError
from quail.analysis.field import Field
from quail.analysis.group import G0, GroupExpr
from quail.analysis.ranking import Ranking
from quail.analysis.unit import Unit, entries


def retrieve(
    unit: Any = entries,
    group: GroupExpr = G0,
    limit: int = 1,
    order: str = "top",
    rank: Ranking | None = None,
) -> list[Any]:
    del unit, group, limit, order, rank
    raise QuailRuntimeError("retrieve() is not wired yet; engine/worker come next")


def count(unit: Unit = entries, group: GroupExpr = G0) -> int:
    del unit, group
    raise QuailRuntimeError("count() is not wired yet; engine/worker come next")


def create_field(field: str | Field) -> Field:
    del field
    raise QuailRuntimeError("create_field() is not wired yet; engine/worker come next")


def tag(group: GroupExpr | list[Any], field: Field, value: Any) -> None:
    del group, field, value
    raise QuailRuntimeError("tag() is not wired yet; engine/worker come next")


def untag(group: GroupExpr | list[Any], field: Field, value: Any | None = None) -> None:
    del group, field, value
    raise QuailRuntimeError("untag() is not wired yet; engine/worker come next")


def quail_print(*values: Any, sep: str = " ", end: str = "\n") -> None:
    del values, sep, end
    raise QuailRuntimeError("print() is not wired yet; worker comes next")


def api_namespace() -> dict[str, Any]:
    """Names injected into quail_exec (minus safe Python builtins)."""

    from quail.analysis.entry import Entry
    from quail.analysis.errors import (
        QuailError,
        QuailFieldError,
        QuailRuntimeError as RuntimeErr,
        QuailScopeError,
        QuailSyntaxError,
    )
    from quail.analysis.expression import Expression
    from quail.analysis.field import Field
    from quail.analysis.group import G0, G1, GroupExpr
    from quail.analysis.operations import (
        AsNumber,
        AsText,
        Length,
        Lexical,
        RegexFindAll,
        RegexSearch,
        RegexSub,
        Semantic,
        Slice,
        Value,
    )
    from quail.analysis.predicate import Predicate
    from quail.analysis.ranking import Ranking
    from quail.analysis.re_helper import ReFacade
    from quail.analysis.unit import Unit, entries, fields

    return {
        "retrieve": retrieve,
        "count": count,
        "create_field": create_field,
        "tag": tag,
        "untag": untag,
        "print": quail_print,
        "G0": G0,
        "G1": G1,
        "entries": entries,
        "fields": fields,
        "Field": Field,
        "Unit": Unit,
        "Expression": Expression,
        "Predicate": Predicate,
        "GroupExpr": GroupExpr,
        "Ranking": Ranking,
        "Entry": Entry,
        "Value": Value,
        "AsText": AsText,
        "AsNumber": AsNumber,
        "RegexSearch": RegexSearch,
        "RegexFindAll": RegexFindAll,
        "RegexSub": RegexSub,
        "Slice": Slice,
        "Length": Length,
        "Lexical": Lexical,
        "Semantic": Semantic,
        "re": ReFacade(),
        "QuailError": QuailError,
        "QuailSyntaxError": QuailSyntaxError,
        "QuailScopeError": QuailScopeError,
        "QuailFieldError": QuailFieldError,
        "QuailRuntimeError": RuntimeErr,
    }
