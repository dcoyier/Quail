"""Expressions: field + operation pipeline."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, NoReturn

from quail.analysis.errors import QuailSyntaxError
from quail.analysis.field import Field
from quail.analysis.operations import Operation, validate_operation_pipeline
from quail.analysis.predicate import Predicate


@dataclass(frozen=True, slots=True)
class Expression:
    root: Field
    operations: tuple[Operation, ...]

    def __init__(self, input: Field | Expression, *operations: Operation) -> None:
        if isinstance(input, Field):
            root = input
            pipeline = list(operations)
        elif isinstance(input, Expression):
            root = input.root
            pipeline = list(input.operations) + list(operations)
        else:
            raise QuailSyntaxError("Expression input must be a Field or Expression")
        if not all(isinstance(operation, Operation) for operation in pipeline):
            raise QuailSyntaxError("Expression operations must be Operation objects")
        while pipeline and pipeline[0].kind == "Value":
            pipeline.pop(0)
        pipeline_tuple = tuple(pipeline)
        validate_operation_pipeline(pipeline_tuple)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "operations", pipeline_tuple)

    @property
    def input(self) -> Field | Expression:
        """Public alias matching the API docs (root field or nested expression)."""

        if len(self.operations) == 0:
            return self.root
        # Reconstruct is unnecessary for evaluation; expose root as documented input.
        return self.root

    def to_record(self) -> dict[str, Any]:
        return {
            "kind": "Expression",
            "root": self.root.to_record(),
            "operations": [operation.to_record() for operation in self.operations],
        }

    def _compare(self, operator: str, other: Any) -> Predicate:
        return Predicate(self, operator, other)

    def __lt__(self, other: Any) -> Predicate:
        return self._compare("<", other)

    def __le__(self, other: Any) -> Predicate:
        return self._compare("<=", other)

    def __gt__(self, other: Any) -> Predicate:
        return self._compare(">", other)

    def __ge__(self, other: Any) -> Predicate:
        return self._compare(">=", other)

    def __eq__(self, other: Any) -> Predicate:  # type: ignore[override]
        return self._compare("==", other)

    def __ne__(self, other: Any) -> Predicate:  # type: ignore[override]
        return self._compare("!=", other)

    def __add__(self, other: Expression | Any) -> Any:
        from quail.analysis.ranking import Ranking

        return Ranking(expression=self) + other

    def __mul__(self, weight: int | float) -> Any:
        from quail.analysis.ranking import Ranking

        return Ranking(expression=self) * weight

    def __rmul__(self, weight: Any) -> NoReturn:
        del weight
        raise QuailSyntaxError("Ranking weight must be on the right: expression * weight")

    def __bool__(self) -> bool:
        raise QuailSyntaxError(
            "Compare Expression objects explicitly; do not use them in if/while/and/or"
        )

    def __iter__(self) -> Iterator[Any]:
        raise QuailSyntaxError("Expression values are symbolic; do not iterate them")
