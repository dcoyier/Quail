"""Where this file sits in Quail

Facade flow (what exists today)::

    agent code
      -> builds symbolic recipes (Field, Expression, Predicate, …)
      -> later: retrieve/tag (engine) reads Turso-backed data

``literals.py`` is only used in the *middle of recipe building*, not by Turso
and not by the engine. Example::

    Expression(Field("topic"), Value()) == ["climate", "policy"]
                                          ^^^^^^^^^^^^^^^^^^^^^^^
                                          this Python value is sealed here
                                          before it is stored on the Predicate

Call chain for that ``==``::

    Expression.__eq__
      -> Predicate(...)
      -> seal_literal(["climate", "policy"])   # this module
      -> Predicate stores the sealed copy

Same idea for Operation params (``RegexSearch("…")``, ``Lexical("…")``, …):
``operations.py`` calls ``seal_mapping`` so params on the Operation are fixed.

So: Field/Expression/Group describe *structure*; this file only handles the
plain data *constants* hanging off that structure (comparison RHS, op kwargs).

Sealing means: check JSON-like shape, deep-copy into an immutable snapshot,
attach snapshot to the AST. Caller mutations afterward cannot change the recipe.

``literal_as_plain`` is read-side only (e.g. ``to_record()``): make a normal
list/dict copy for display. The AST keeps its sealed snapshot.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from quail.analysis.errors import QuailSyntaxError

# Reject integers whose absolute value has this many digits or more.
_MAX_INT_DIGITS = 100


def seal_literal(value: Any, *, _stack: set[int] | None = None) -> Any:
    """Validate ``value`` and return an immutable deep copy safe to store on the AST.

    ``_stack`` tracks object ids we are currently inside, so a cycle like
    ``a.append(a)`` is rejected instead of recursing forever.
    """

    stack = set() if _stack is None else _stack

    # --- immutable scalars: already safe to store as-is ---
    if value is None or isinstance(value, str | bool):
        return value

    # bool is a subclass of int in Python; keep True/False out of the int path.
    if isinstance(value, int) and not isinstance(value, bool):
        _require_bounded_int(value)
        return value

    if isinstance(value, float):
        _require_finite_float(value)
        return value

    # --- containers: copy deeply, detect cycles, seal children ---
    if isinstance(value, Mapping):
        return _seal_mapping_with_cycle_guard(value, stack)

    if isinstance(value, list | tuple):
        return _seal_sequence_with_cycle_guard(value, stack)

    raise QuailSyntaxError(f"Symbolic values do not support {type(value).__name__}")


def seal_mapping(
    value: Mapping[Any, Any],
    *,
    _stack: set[int] | None = None,
) -> MappingProxyType[str, Any]:
    """Seal a dict-like value: string keys only, immutable mapping result."""

    stack = set() if _stack is None else _stack
    return _seal_mapping_body(value, stack)


def literal_as_plain(value: Any) -> Any:
    """Return a detached plain list/dict copy of a sealed literal (for records/UI).

    The AST node keeps its sealed snapshot; this only builds a normal Python
    value for callers that prefer lists over tuples.
    """

    if isinstance(value, Mapping):
        return {key: literal_as_plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [literal_as_plain(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _require_bounded_int(value: int) -> None:
    if abs(value) >= 10**_MAX_INT_DIGITS:
        raise QuailSyntaxError("Symbolic integers are too large")


def _require_finite_float(value: float) -> None:
    if not math.isfinite(value):
        raise QuailSyntaxError("Symbolic values cannot contain non-finite floats")


def _require_string_key(key: Any) -> str:
    if not isinstance(key, str):
        raise QuailSyntaxError("Symbolic value dict keys must be strings")
    return key


# ---------------------------------------------------------------------------
# Cycle-safe container sealing
# ---------------------------------------------------------------------------


def _enter(stack: set[int], container: Any) -> int:
    """Mark ``container`` as in-progress; error if we are already inside it."""

    identity = id(container)
    if identity in stack:
        raise QuailSyntaxError("Symbolic values cannot contain cycles")
    stack.add(identity)
    return identity


def _seal_mapping_with_cycle_guard(
    value: Mapping[Any, Any],
    stack: set[int],
) -> MappingProxyType[str, Any]:
    identity = _enter(stack, value)
    try:
        return _seal_mapping_body(value, stack)
    finally:
        # Leave so the same object can appear again as a sibling, not an ancestor.
        stack.remove(identity)


def _seal_sequence_with_cycle_guard(
    value: list[Any] | tuple[Any, ...],
    stack: set[int],
) -> tuple[Any, ...]:
    identity = _enter(stack, value)
    try:
        # list -> tuple so the sequence itself cannot be appended to later.
        return tuple(seal_literal(item, _stack=stack) for item in value)
    finally:
        stack.remove(identity)


def _seal_mapping_body(
    value: Mapping[Any, Any],
    stack: set[int],
) -> MappingProxyType[str, Any]:
    sealed: dict[str, Any] = {}
    for key, item in value.items():
        sealed[_require_string_key(key)] = seal_literal(item, _stack=stack)
    # MappingProxyType: read-only view — no item assignment after seal.
    return MappingProxyType(sealed)
