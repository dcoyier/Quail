"""Inert, typed recipes for the four verbs.

Nodes have ordinary structural equality; public expressions deliberately do not.
The scope only supplies a cached catalog, so constructing a recipe cannot scan
rows, prepare a search, or request embeddings.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Literal, NoReturn, Protocol

from quail.contracts import QuailError, canonical_json, json_value
from quail.language.scalars import regex

type Kind = Literal["any", "text", "number", "list", "predicate"]


class Scope(Protocol):
    def field_kind(self, name: str) -> Kind: ...


@dataclass(frozen=True)
class Node:
    op: str
    kind: Kind
    args: tuple[Node, ...] = ()
    # Literal containers use canonical JSON here, keeping node keys immutable.
    data: tuple[str | int | None, ...] = ()

    @property
    def fields(self) -> frozenset[str]:
        if self.op == "field":
            return frozenset((str(self.data[0]),))
        return frozenset().union(*(arg.fields for arg in self.args))


@dataclass(frozen=True)
class Method:
    accepts: frozenset[Kind]
    produces: Kind | Literal["same"]


METHODS: dict[str, Method] = {
    "text": Method(frozenset({"any", "text", "number", "list"}), "text"),
    "number": Method(frozenset({"any", "text", "number"}), "number"),
    "length": Method(frozenset({"any", "text", "list"}), "number"),
    "lower": Method(frozenset({"any", "text"}), "text"),
    "upper": Method(frozenset({"any", "text"}), "text"),
    "strip": Method(frozenset({"any", "text"}), "text"),
    "search": Method(frozenset({"any", "text"}), "text"),
    "findall": Method(frozenset({"any", "text"}), "list"),
    "sub": Method(frozenset({"any", "text", "list"}), "same"),
    "slice": Method(frozenset({"any", "text", "list"}), "same"),
    "isin": Method(frozenset({"any", "text", "number"}), "predicate"),
    "contains": Method(frozenset({"any", "text", "list"}), "predicate"),
    "lexical": Method(frozenset({"any", "text"}), "number"),
    "semantic": Method(frozenset({"any", "text"}), "number"),
}


def literal(value: object) -> Node:
    checked = json_value(value)
    kind: Kind = "any"
    if isinstance(checked, str):
        kind = "text"
    elif isinstance(checked, (int, float)):
        kind = "number"
    elif isinstance(checked, list):
        kind = "list"
    return Node("literal", kind, data=(canonical_json(checked),))


class Expression:
    __slots__ = ("_scope", "_node")

    def __init__(self, scope: Scope, node: Node) -> None:
        self._scope = scope
        self._node = node

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._node.op}, produces={self._node.kind!r})"

    def __bool__(self) -> NoReturn:
        raise QuailError(
            "An expression has no Python truth value",
            "Use &, |, and ~ for predicates; call a verb to evaluate them",
        )

    def __iter__(self) -> NoReturn:
        raise QuailError(
            "An expression is not iterable", "Use values(expr), .isin(), or .contains()"
        )

    def _operand(self, other: object) -> Node:
        if isinstance(other, Expression):
            if other._scope is not self._scope:
                raise QuailError("Expressions belong to different kernels")
            return other._node
        return literal(other)

    def _method(self, name: str, *data: str | int | None) -> Expression:
        method = METHODS[name]
        if self._node.kind not in method.accepts:
            raise QuailError(f".{name}() does not accept {self._node.kind} expressions")
        kind = self._node.kind if method.produces == "same" else method.produces
        node = Node(name, kind, (self._node,), data)
        return (
            Predicate(self._scope, node) if kind == "predicate" else Expression(self._scope, node)
        )

    def text(self) -> Expression:
        return self._method("text")

    def number(self) -> Expression:
        return self._method("number")

    def length(self) -> Expression:
        return self._method("length")

    def lower(self) -> Expression:
        return self._method("lower")

    def upper(self) -> Expression:
        return self._method("upper")

    def strip(self) -> Expression:
        return self._method("strip")

    def search(self, pattern: str, flags: int = 0) -> Expression:
        regex(pattern, flags)
        return self._method("search", pattern, int(flags))

    def findall(self, pattern: str, flags: int = 0) -> Expression:
        regex(pattern, flags)
        return self._method("findall", pattern, int(flags))

    def sub(self, pattern: str, repl: str, flags: int = 0) -> Expression:
        regex(pattern, flags)
        if not isinstance(repl, str):
            raise QuailError("Regex replacement must be text")
        return self._method("sub", pattern, repl, int(flags))

    def slice(self, start: int | None, end: int | None = None) -> Expression:
        if any(value is not None and type(value) is not int for value in (start, end)):
            raise QuailError("Slice boundaries must be integers or None")
        return self._method("slice", start, end)

    def isin(self, values: list[object]) -> Predicate:
        checked = json_value(values)
        if not isinstance(checked, list) or any(isinstance(v, (list, dict)) for v in checked):
            raise QuailError(".isin() expects a list of scalar values")
        result = self._method("isin", canonical_json(checked))
        assert isinstance(result, Predicate)
        return result

    def contains(self, value: object) -> Predicate:
        result = self._method("contains", canonical_json(json_value(value)))
        assert isinstance(result, Predicate)
        return result

    def _search(self, method: str, query: str) -> Expression:
        if self._node.op != "field" or self._node.data[0] == "id":
            raise QuailError(
                f".{method}() requires a stored, non-ID Field",
                "Tag a transformed value first, then search that tag field",
            )
        if not isinstance(query, str) or not query.strip():
            raise QuailError("Search query must be non-empty text")
        json_value(query)
        return self._method(method, query)

    def lexical(self, query: str) -> Expression:
        return self._search("lexical", query)

    def semantic(self, query: str) -> Expression:
        return self._search("semantic", query)

    def _compare(self, operation: str, other: object) -> Predicate:
        return Predicate(
            self._scope, Node(operation, "predicate", (self._node, self._operand(other)))
        )

    # Symbolic equality intentionally returns a predicate rather than object.__eq__'s bool.
    def __eq__(self, other: object) -> Predicate:  # type: ignore[override]
        return self._compare("eq", other)

    def __ne__(self, other: object) -> Predicate:  # type: ignore[override]
        return self._compare("ne", other)

    def __lt__(self, other: object) -> Predicate:
        return self._compare("lt", other)

    def __le__(self, other: object) -> Predicate:
        return self._compare("le", other)

    def __gt__(self, other: object) -> Predicate:
        return self._compare("gt", other)

    def __ge__(self, other: object) -> Predicate:
        return self._compare("ge", other)

    def _arithmetic(self, operation: str, other: object, *, reverse: bool = False) -> Expression:
        right = self._operand(other)
        if self._node.kind != "number" or right.kind != "number":
            raise QuailError(
                "Arithmetic requires number expressions", "Convert with .number() first"
            )
        args = (right, self._node) if reverse else (self._node, right)
        return Expression(self._scope, Node(operation, "number", args))

    def __add__(self, other: object) -> Expression:
        return self._arithmetic("add", other)

    def __radd__(self, other: object) -> Expression:
        return self._arithmetic("add", other, reverse=True)

    def __sub__(self, other: object) -> Expression:
        return self._arithmetic("subtract", other)

    def __rsub__(self, other: object) -> Expression:
        return self._arithmetic("subtract", other, reverse=True)

    def __mul__(self, other: object) -> Expression:
        return self._arithmetic("multiply", other)

    def __rmul__(self, other: object) -> Expression:
        return self._arithmetic("multiply", other, reverse=True)

    def __truediv__(self, other: object) -> Expression:
        return self._arithmetic("divide", other)

    def __rtruediv__(self, other: object) -> Expression:
        return self._arithmetic("divide", other, reverse=True)

    def __neg__(self) -> Expression:
        return self._arithmetic("multiply", -1)


class Predicate(Expression):
    def _combine(self, operation: str, other: object) -> Predicate:
        if not isinstance(other, Predicate):
            raise QuailError("Combine predicates with other predicates, not Python booleans")
        return Predicate(
            self._scope, Node(operation, "predicate", (self._node, self._operand(other)))
        )

    def __and__(self, other: object) -> Predicate:
        return self._combine("and", other)

    def __or__(self, other: object) -> Predicate:
        return self._combine("or", other)

    def __invert__(self) -> Predicate:
        return Predicate(self._scope, Node("not", "predicate", (self._node,)))


class Field(Expression):
    def __init__(self, scope: Scope, name: str) -> None:
        if not isinstance(name, str):
            raise QuailError("Field name must be text")
        super().__init__(scope, Node("field", scope.field_kind(name), data=(name,)))


class Random(Expression):
    def __init__(self, scope: Scope, seed: object = None) -> None:
        if seed is not None and not isinstance(seed, (int, float, str, bytes, bytearray)):
            raise QuailError("Random seed must be None, a number, text, bytes, or bytearray")
        if isinstance(seed, float) and not math.isfinite(seed):
            raise QuailError("Random numeric seeds must be finite")
        salt = random.Random(seed).getrandbits(128)
        super().__init__(scope, Node("random", "number", data=(str(salt),)))


def constructors(scope: Scope) -> tuple[type[Field], type[Random]]:
    """Bind ordinary constructor classes to one evaluator without global state."""

    class BoundField(Field):
        def __init__(self, name: str) -> None:
            super().__init__(scope, name)

    class BoundRandom(Random):
        def __init__(self, seed: object = None) -> None:
            super().__init__(scope, seed)

    BoundField.__name__ = BoundField.__qualname__ = "Field"
    BoundRandom.__name__ = BoundRandom.__qualname__ = "Random"
    return BoundField, BoundRandom
