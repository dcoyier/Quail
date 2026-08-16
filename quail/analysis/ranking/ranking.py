"""Ranking: symbolic ordering over entry scores."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, NoReturn

from quail.analysis.errors import QuailSyntaxError
from quail.analysis.expression import Expression
from quail.analysis.operations import final_pipeline_kind


def _is_rankable(expression: Expression) -> bool:
    return final_pipeline_kind(expression.operations) in ("number", "score")


@dataclass(frozen=True, slots=True)
class Ranking:
    expression: Expression | None = None
    left: Ranking | None = None
    operator: str | None = None
    right: Any = None

    def __post_init__(self) -> None:
        if (
            self.expression is None
            and self.left is None
            and self.operator is None
            and self.right is None
        ):
            return
        if self.expression is not None:
            if self.left is not None or self.operator is not None or self.right is not None:
                raise QuailSyntaxError("Ranking expression variant accepts only an Expression")
            if not isinstance(self.expression, Expression):
                raise QuailSyntaxError("Ranking expression must be an Expression")
            if not _is_rankable(self.expression):
                raise QuailSyntaxError(
                    "Rankable expressions must end in AsNumber(), Length(), Lexical(), or Semantic()"
                )
            return
        if not isinstance(self.left, Ranking):
            raise QuailSyntaxError("Ranking composition requires a left Ranking")
        if self.operator == "+":
            if not isinstance(self.right, Ranking):
                raise QuailSyntaxError("Ranking addition requires a right Ranking")
        elif self.operator == "*":
            object.__setattr__(self, "right", _ranking_weight(self.right))
        else:
            raise QuailSyntaxError("Ranking must select exactly one supported variant")

    def to_record(self) -> dict[str, Any]:
        if self.expression is None and self.left is None:
            return {
                "empty": True,
                "expression": None,
                "left": None,
                "operator": None,
                "right": None,
            }
        return {
            "empty": False,
            "expression": None if self.expression is None else self.expression.to_record(),
            "left": None if self.left is None else self.left.to_record(),
            "operator": self.operator,
            "right": self.right.to_record() if isinstance(self.right, Ranking) else self.right,
        }

    def __add__(self, other: Ranking | Expression) -> Ranking:
        if self.expression is None and self.left is None:
            return _as_ranking(other)
        right = _as_ranking(other)
        if right.expression is None and right.left is None:
            return self
        return Ranking(left=self, operator="+", right=right)

    def __mul__(self, weight: int | float) -> Ranking:
        if self.expression is None and self.left is None:
            raise QuailSyntaxError("Cannot weight an empty Ranking")
        return Ranking(left=self, operator="*", right=weight)

    def __rmul__(self, weight: Any) -> NoReturn:
        del weight
        raise QuailSyntaxError("Ranking weight must be on the right: ranking * weight")


def _ranking_weight(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise QuailSyntaxError("Ranking weight must be a finite non-negative number")
    weight = float(value)
    if not math.isfinite(weight) or weight < 0:
        raise QuailSyntaxError("Ranking weight must be a finite non-negative number")
    return weight


def _as_ranking(value: Ranking | Expression) -> Ranking:
    if isinstance(value, Ranking):
        return value
    if isinstance(value, Expression):
        return Ranking(expression=value)
    raise QuailSyntaxError("Ranking composition requires a Ranking or Expression")
