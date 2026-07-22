"""Symbolic analysis language (model-facing AST).

Mental model (no execution yet):

    Expression(Field("theme"), Value()) == "trust"
        -> Predicate
        -> G0.where(...)
        -> GroupExpr

Types here are immutable descriptions. Side effects (retrieve, tag, print)
arrive later with the worker — they are not methods on these types.

Deferred vs v0.10: regex ops, Lexical/Semantic, ReFacade, G1, Ranking,
field/value Units, Entry.value runtime reads.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, NoReturn

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class QuailError(Exception):
    """Base error for the analysis language."""


class QuailSyntaxError(QuailError):
    """Invalid symbolic construction or operator use."""


class QuailScopeError(QuailError):
    """Valid syntax used in the wrong scope (e.g. where on fields)."""


# ---------------------------------------------------------------------------
# Literals (comparison RHS, operation params)
# ---------------------------------------------------------------------------


def freeze_value(value: Any, _active: set[int] | None = None) -> Any:
    """Deep-freeze a JSON-ish value for storage inside symbolic nodes."""

    active = set() if _active is None else _active
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) >= 10**100:
            raise QuailSyntaxError("Symbolic integers are too large")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QuailSyntaxError("Symbolic values cannot contain non-finite floats")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise QuailSyntaxError("Symbolic values cannot contain cycles")
        active.add(identity)
        try:
            return freeze_mapping(value, active)
        finally:
            active.remove(identity)
    if isinstance(value, list | tuple):
        identity = id(value)
        if identity in active:
            raise QuailSyntaxError("Symbolic values cannot contain cycles")
        active.add(identity)
        try:
            return tuple(freeze_value(item, active) for item in value)
        finally:
            active.remove(identity)
    raise QuailSyntaxError(f"Symbolic values do not support {type(value).__name__}")


def freeze_mapping(
    value: Mapping[Any, Any],
    _active: set[int] | None = None,
) -> MappingProxyType[str, Any]:
    active = set() if _active is None else _active
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise QuailSyntaxError("Symbolic value dict keys must be strings")
        result[key] = freeze_value(item, active)
    return MappingProxyType(result)


def thaw_value(value: Any) -> Any:
    """Undo freeze_value for reading params/records."""

    if isinstance(value, Mapping):
        return {key: thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_value(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------


@dataclass(eq=False, frozen=True, slots=True)
class Field:
    """Named column/tag reference. Compare values via Expression(...), not Field."""

    name: str
    kind: str | None = None  # "source" | "analysis" | None (unspecified)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise QuailSyntaxError("Field name must be a non-empty string")
        if self.kind not in (None, "source", "analysis"):
            raise QuailSyntaxError("Field kind must be source, analysis, or None")

    def to_record(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Field):
            raise QuailSyntaxError(
                "Field comparisons are not supported; compare values with "
                f"Expression(Field({self.name!r}), Value())"
            )
        return (self.name, self.kind) == (other.name, other.kind)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash((self.name, self.kind))

    def _reject_ordering(self) -> NoReturn:
        raise QuailSyntaxError(
            "Field ordering is not supported; compare a numeric expression such as "
            f"Expression(Field({self.name!r}), AsNumber())"
        )

    def __lt__(self, other: object) -> NoReturn:
        del other
        self._reject_ordering()

    def __le__(self, other: object) -> NoReturn:
        del other
        self._reject_ordering()

    def __gt__(self, other: object) -> NoReturn:
        del other
        self._reject_ordering()

    def __ge__(self, other: object) -> NoReturn:
        del other
        self._reject_ordering()


@dataclass(frozen=True, slots=True)
class Unit:
    """What to retrieve: for now only entries (fields/values later)."""

    scope: str
    field: Field | None = None

    def __post_init__(self) -> None:
        if self.scope != "entries":
            raise QuailSyntaxError('Unit scope must be "entries" in this build')
        if self.field is not None:
            raise QuailSyntaxError('Unit("entries") does not take a field')

    def to_record(self) -> dict[str, Any]:
        return {"scope": self.scope, "field": None}


@dataclass(frozen=True, slots=True)
class Operation:
    """One step in an Expression pipeline (Value, AsText, AsNumber, …)."""

    kind: str
    params: MappingProxyType[str, Any]

    def __init__(self, kind: str, params: dict[str, Any] | None = None) -> None:
        if not isinstance(kind, str) or not kind:
            raise QuailSyntaxError("Operation kind must be a non-empty string")
        if params is not None and not isinstance(params, dict):
            raise QuailSyntaxError("Operation params must be a dict or None")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "params", freeze_mapping({} if params is None else params))

    def to_record(self) -> dict[str, Any]:
        return {"kind": self.kind, **thaw_value(self.params)}


_ALLOWED_OPS = frozenset({"Value", "AsText", "AsNumber"})


def validate_operation_pipeline(operations: tuple[Operation, ...]) -> None:
    if not operations:
        raise QuailSyntaxError("Expression requires at least one operation")
    if operations[0].kind != "Value":
        raise QuailSyntaxError("Expression pipelines must start with Value()")
    for operation in operations:
        if operation.kind not in _ALLOWED_OPS:
            raise QuailSyntaxError(f"Unsupported operation {operation.kind!r} in this build")


@dataclass(frozen=True, slots=True)
class Expression:
    """Field + operation pipeline. Comparisons build Predicates."""

    root: Field
    operations: tuple[Operation, ...]

    def __init__(self, input: Field | Expression, *operations: Operation) -> None:
        if isinstance(input, Field):
            root = input
            pipeline = tuple(operations)
        elif isinstance(input, Expression):
            root = input.root
            pipeline = input.operations + tuple(operations)
        else:
            raise QuailSyntaxError("Expression input must be a Field or Expression")
        if not all(isinstance(operation, Operation) for operation in pipeline):
            raise QuailSyntaxError("Expression operations must be Operation objects")
        validate_operation_pipeline(pipeline)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "operations", pipeline)

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

    def __bool__(self) -> bool:
        raise QuailSyntaxError(
            "Compare Expression objects explicitly; do not use them in if/while/and/or"
        )

    def __iter__(self) -> Iterator[Any]:
        raise QuailSyntaxError("Expression values are symbolic; do not iterate them")


def _comparison_right(operator: str, right: Any) -> Any:
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
        return freeze_value(right)
    if isinstance(right, Expression):
        return right
    return freeze_value(right)


@dataclass(frozen=True, slots=True)
class Predicate:
    """Filter AST: comparison, &, |, ~ (not Python and/or/not)."""

    left: Expression | Predicate
    operator: str
    right: Any

    def __init__(self, left: Any, operator: str, right: Any = None) -> None:
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


def _operand_record(value: Any) -> Any:
    if hasattr(value, "to_record"):
        return value.to_record()
    return thaw_value(value)


@dataclass(frozen=True, slots=True)
class GroupExpr:
    """Entry population: G0, .where(predicate), or & | ~ composition."""

    scope: str
    name: str | None = None
    predicate: Predicate | None = None
    left: GroupExpr | None = None
    operator: str | None = None
    right: GroupExpr | None = None

    def __init__(
        self,
        scope: str,
        *,
        name: str | None = None,
        predicate: Predicate | None = None,
        left: GroupExpr | None = None,
        operator: str | None = None,
        right: GroupExpr | None = None,
    ) -> None:
        if scope != "entries":
            raise QuailSyntaxError('GroupExpr scope must be "entries" in this build')
        if predicate is not None and not isinstance(predicate, Predicate):
            raise QuailSyntaxError("GroupExpr predicate must be a Predicate")
        if operator is not None and operator not in ("and", "or", "not"):
            raise QuailSyntaxError("GroupExpr operator must be and, or, or not")

        named = name is not None
        filtered = predicate is not None
        composed = operator is not None or left is not None or right is not None
        form_count = sum([named, filtered, composed])
        if form_count == 0:
            raise QuailSyntaxError("GroupExpr requires name=, predicate=, or composition")
        if form_count > 1:
            raise QuailSyntaxError("GroupExpr accepts exactly one population form")

        if name is not None and name != "G0":
            raise QuailSyntaxError('Built-in entry group must be named "G0"')
        if composed:
            if not isinstance(left, GroupExpr) or left.scope != scope:
                raise QuailScopeError("Group composition requires a compatible left GroupExpr")
            if operator == "not":
                if right is not None:
                    raise QuailSyntaxError("Group inversion accepts only a left GroupExpr")
            elif operator in ("and", "or"):
                if not isinstance(right, GroupExpr) or right.scope != scope:
                    raise QuailScopeError(
                        "Group composition requires a compatible right GroupExpr"
                    )
            else:
                raise QuailSyntaxError("GroupExpr composition requires and, or, or not")

        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "predicate", predicate)
        object.__setattr__(self, "left", left)
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "right", right)

    def where(self, predicate: Predicate) -> GroupExpr:
        if not isinstance(predicate, Predicate):
            raise QuailSyntaxError(
                "where(...) requires a Predicate created by comparing an Expression"
            )
        return self & GroupExpr(scope="entries", predicate=predicate)

    def to_record(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "name": self.name,
            "predicate": None if self.predicate is None else self.predicate.to_record(),
            "left": None if self.left is None else self.left.to_record(),
            "operator": self.operator,
            "right": None if self.right is None else self.right.to_record(),
        }

    def __and__(self, other: GroupExpr) -> GroupExpr:
        return GroupExpr(scope=self.scope, left=self, operator="and", right=other)

    def __or__(self, other: GroupExpr) -> GroupExpr:
        return GroupExpr(scope=self.scope, left=self, operator="or", right=other)

    def __invert__(self) -> GroupExpr:
        return GroupExpr(scope=self.scope, left=self, operator="not")

    def __bool__(self) -> bool:
        raise QuailSyntaxError("Compose groups with &, |, and ~; do not use them in if/while")

    def __iter__(self) -> Iterator[Any]:
        raise QuailSyntaxError("GroupExpr values are symbolic; do not iterate them")


_ENTRY_TOKEN = object()


@dataclass(frozen=True, slots=True)
class Entry:
    """Opaque handle for one dataset row. Issued by the runtime, not user code."""

    entry_id: str

    def __init__(self, entry_id: str, token: object | None = None) -> None:
        if token is not _ENTRY_TOKEN:
            raise QuailSyntaxError("Entry handles are created by Quail, not user code")
        if not isinstance(entry_id, str) or not entry_id:
            raise QuailSyntaxError("Entry id must be a non-empty string")
        object.__setattr__(self, "entry_id", entry_id)

    def to_record(self) -> dict[str, Any]:
        return {"entry_id": self.entry_id}


def make_entry(entry_id: str) -> Entry:
    """Host/runtime helper to mint an Entry handle."""

    return Entry(entry_id, _ENTRY_TOKEN)


# ---------------------------------------------------------------------------
# Factories and builtins
# ---------------------------------------------------------------------------


def Value() -> Operation:
    return Operation("Value")


def AsText() -> Operation:
    return Operation("AsText")


def AsNumber() -> Operation:
    return Operation("AsNumber")


G0 = GroupExpr(scope="entries", name="G0")
entries = Unit(scope="entries")
