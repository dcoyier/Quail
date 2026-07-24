"""Injected quail_exec namespace inventory (worker builds the live callables)."""

from __future__ import annotations

from typing import Any

from quail.analysis.bindings import RESERVED_NAMES
from quail.analysis.entry import Entry
from quail.analysis.errors import (
    QuailError,
    QuailFieldError,
    QuailRuntimeError,
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


def api_namespace() -> dict[str, Any]:
    """Documented injected names (types and factories; no live retrieve/tag)."""

    return {
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
        "QuailRuntimeError": QuailRuntimeError,
    }


__all__ = [
    "RESERVED_NAMES",
    "api_namespace",
]
