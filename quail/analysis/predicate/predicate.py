"""Predicates: symbolic true/false recipes per entry."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from quail.analysis.errors import QuailSyntaxError
from quail.analysis.literals import literal_as_plain, seal_literal


@dataclass(frozen=True, slots=True)
class Predicate:
    left: Any
    operator: str
    right: Any

    def __init__(self, left: Any, operator: str, right: Any = None) -> None:
        from quail.analysis.expression import Expression

        if operator in ("and", "or"):
            if not isinstance(left, Predicate) or not isinstance(right, Predicate):
                raise QuailSyntaxError("Predicate composition requires Predicate operands")
            stored_right: Any = right
        elif operator == "not":
            if not isinstance(left, Predicate) or right is not None:
                raise QuailSyntaxError("Predicate inversion requires one Predicate operand")
            stored_right = None
        elif operator in ("<", "<=", ">", ">=", "==", "!="):
            if not isinstance(left, Expression):
                raise QuailSyntaxError("Predicate comparisons require an Expression left operand")
            stored_right = _comparison_right(operator, right)
        else:
            raise QuailSyntaxError("Unsupported Predicate operator")
        object.__setattr__(self, "left", left)
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "right", stored_right)

    def to_record(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": "Predicate",
            "operator": self.operator,
            "left": _operand_record(self.left),
        }
        if self.operator != "not":
            result["right"] = _operand_record(self.right)
        return result

    def __and__(self, other: Predicate) -> Predicate:
        return Predicate(self, "and", other)

    def __or__(self, other: Predicate) -> Predicate:
        return Predicate(self, "or", other)

    def __invert__(self) -> Predicate:
        return Predicate(self, "not")

    def __bool__(self) -> bool:
        raise QuailSyntaxError(
            "Compose predicates with &, |, and ~; then select entries with G0.where(...)"
        )

    def __add__(self, other: Any) -> NoReturn:
        del other
        raise QuailSyntaxError("Predicates do not support +")


def _comparison_right(operator: str, right: Any) -> Any:
    from quail.analysis.expression import Expression

    if operator in ("<", "<=", ">", ">="):
        if isinstance(right, Expression):
            return right
        if isinstance(right, bool) or not isinstance(right, int | float):
            raise QuailSyntaxError(
                "Ordering comparisons require a finite numeric literal or Expression"
            )
        if isinstance(right, float) and not math.isfinite(right):
            raise QuailSyntaxError(
                "Ordering comparisons require a finite numeric literal or Expression"
            )
        return seal_literal(right)
    if isinstance(right, Expression):
        return right
    return seal_literal(right)


def _operand_record(value: Any) -> Any:
    if hasattr(value, "to_record"):
        return value.to_record()
    if isinstance(value, Mapping | tuple):
        return literal_as_plain(value)
    return value
