"""Encode and decode session bindings for quail_exec persistence."""

from __future__ import annotations

import keyword
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from quail.analysis.entry import Entry
from quail.analysis.errors import QuailError, QuailRuntimeError, QuailSyntaxError
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import G0, G1, GroupExpr
from quail.analysis.operations import Operation
from quail.analysis.predicate import Predicate
from quail.analysis.ranking import Ranking
from quail.analysis.re_helper import ReFacade
from quail.analysis.search_text.records import (
    entry_from_record,
    expression_from_record,
    field_from_record,
    operation_from_record,
    predicate_from_record,
)
from quail.analysis.unit import Unit
from quail.datasets.hashing import canonical_json, decode_json


BindingKind = Literal[
    "literal",
    "literal_v2",
    "field",
    "unit",
    "operation",
    "expression",
    "predicate",
    "ranking",
    "group_expr",
    "entry",
    "result_list",
]

BINDING_KINDS = frozenset(
    {
        "literal",
        "literal_v2",
        "field",
        "unit",
        "operation",
        "expression",
        "predicate",
        "ranking",
        "group_expr",
        "entry",
        "result_list",
    }
)

MAX_BINDING_NAME_BYTES = 128
MAX_BINDING_DEPTH = 64
MAX_BINDING_ITEMS = 100_000
MAX_INTEGER_DECIMAL_DIGITS = 100
_ORDERED_DICT_KEY = "dict"

RESERVED_NAMES = frozenset(
    {
        "retrieve",
        "count",
        "create_field",
        "tag",
        "untag",
        "print",
        "G0",
        "G1",
        "entries",
        "fields",
        "Field",
        "Unit",
        "Expression",
        "Predicate",
        "GroupExpr",
        "Ranking",
        "Entry",
        "Value",
        "AsText",
        "AsNumber",
        "RegexSearch",
        "RegexFindAll",
        "RegexSub",
        "Slice",
        "Length",
        "Lexical",
        "Semantic",
        "re",
        "QuailError",
        "QuailSyntaxError",
        "QuailScopeError",
        "QuailFieldError",
        "QuailRuntimeError",
    }
)


class BindingEncodingError(QuailSyntaxError):
    """A named user variable cannot be represented as a durable binding."""

    def __init__(self, name: str, message: str) -> None:
        super().__init__(f"Cannot persist binding {name!r}: {message}")
        self.name = name


@dataclass(eq=False, frozen=True, slots=True)
class EncodedBinding:
    """Storage-ready value for one saved Python variable."""

    value_kind: BindingKind
    value: Any

    def __post_init__(self) -> None:
        if not isinstance(self.value_kind, str) or self.value_kind not in BINDING_KINDS:
            raise QuailRuntimeError(f"Unknown binding kind: {self.value_kind}")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EncodedBinding):
            return NotImplemented
        if self.value_kind != other.value_kind:
            return False
        return canonical_binding_bytes(self) == canonical_binding_bytes(other)


def encode_binding_value(value: Any) -> EncodedBinding:
    """Encode one live Python value into a canonical, JSON-safe binding."""

    if isinstance(value, Field):
        return _encoded_binding("field", value.to_record())
    if isinstance(value, Unit):
        return _encoded_binding("unit", value.to_record())
    if isinstance(value, Operation):
        return _encoded_binding("operation", value.to_record())
    if isinstance(value, Expression):
        return _encoded_binding("expression", value.to_record())
    if isinstance(value, Predicate):
        return _encoded_binding("predicate", value.to_record())
    if isinstance(value, Ranking):
        return _encoded_binding("ranking", value.to_record())
    if isinstance(value, GroupExpr):
        return _encoded_binding("group_expr", value.to_record())
    if isinstance(value, Entry):
        return _encoded_binding("entry", value.to_record())
    if isinstance(value, ReFacade) or callable(value):
        raise QuailRuntimeError(f"Cannot persist value of type {type(value).__name__}")
    if value is None or isinstance(value, str | bool | int | float):
        return _encoded_binding("literal", _encode_scalar(value))
    if type(value) is list:
        try:
            return _encoded_binding("literal_v2", encode_ordered_literal(value))
        except QuailRuntimeError:
            return _encoded_binding("result_list", encode_plain_list(value))
    if type(value) is dict:
        return _encoded_binding("literal_v2", encode_ordered_literal(value))
    if isinstance(value, tuple | set | frozenset):
        extra = "; rebind as a list" if isinstance(value, tuple) else ""
        raise QuailRuntimeError(f"Cannot persist value of type {type(value).__name__}{extra}")
    raise QuailRuntimeError(f"Cannot persist value of type {type(value).__name__}")


def decode_binding_value(value_kind: BindingKind | str, value: Any) -> Any:
    """Rehydrate one saved binding after validating its complete wire shape."""

    if not isinstance(value_kind, str) or value_kind not in BINDING_KINDS:
        raise QuailRuntimeError(f"Unknown binding kind: {value_kind}")
    canonical_value = decode_json(canonical_json(value))
    decoders: dict[str, Callable[[Any], Any]] = {
        "literal": _decode_literal,
        "literal_v2": decode_ordered_literal,
        "field": field_from_record,
        "unit": _decode_unit,
        "operation": operation_from_record,
        "expression": expression_from_record,
        "predicate": predicate_from_record,
        "ranking": _decode_ranking,
        "group_expr": _decode_group_expr,
        "entry": entry_from_record,
        "result_list": decode_result_list,
    }
    try:
        return decoders[value_kind](canonical_value)
    except (QuailError, KeyError, TypeError, ValueError, OverflowError, RecursionError) as error:
        if isinstance(error, QuailRuntimeError | QuailSyntaxError):
            raise
        raise QuailRuntimeError(f"Malformed {value_kind} binding: {error}") from error


def encode_namespace(
    namespace: Mapping[str, Any],
    *,
    reserved_names: frozenset[str] = RESERVED_NAMES,
) -> dict[str, EncodedBinding]:
    """Encode every user-created top-level variable in an execution namespace."""

    bindings: dict[str, EncodedBinding] = {}
    for name, value in namespace.items():
        if not isinstance(name, str):
            raise QuailRuntimeError("Namespace binding names must be strings")
        if name in reserved_names or name == "__builtins__":
            continue
        require_namespace_name(name)
        try:
            bindings[name] = encode_binding_value(value)
        except QuailError as error:
            raise BindingEncodingError(name, str(error)) from error
    return bindings


def decode_namespace(bindings: Mapping[str, EncodedBinding]) -> dict[str, Any]:
    """Decode stored binding rows into names ready to inject into a namespace."""

    namespace: dict[str, Any] = {}
    for name, binding in bindings.items():
        require_namespace_name(name)
        if not isinstance(binding, EncodedBinding):
            raise QuailRuntimeError(f"Stored binding for {name!r} must be an EncodedBinding")
        namespace[name] = decode_binding_value(binding.value_kind, binding.value)
    return namespace


def validate_binding_fields(value: Any, check_field: Callable[[Field], None]) -> None:
    """Walk a binding value; call check_field for each live Field (and Field records)."""

    _validate_binding_fields_node(value, check_field, seen=set())


def validate_encoded_bindings(
    bindings: Mapping[str, EncodedBinding],
    check_field: Callable[[Field], None],
) -> None:
    """Decode each binding and validate explicit Field kinds via check_field."""

    for name, binding in bindings.items():
        require_namespace_name(name)
        if not isinstance(binding, EncodedBinding):
            raise QuailRuntimeError(f"Stored binding for {name!r} must be an EncodedBinding")
        validate_binding_fields(
            decode_binding_value(binding.value_kind, binding.value),
            check_field,
        )


def _validate_binding_fields_node(
    value: Any,
    check_field: Callable[[Field], None],
    *,
    seen: set[int],
) -> None:
    if isinstance(value, Field):
        check_field(value)
        return
    if isinstance(value, Expression):
        check_field(value.root)
        for operation in value.operations:
            _validate_binding_fields_node(operation, check_field, seen=seen)
        return
    if isinstance(value, Operation):
        _validate_binding_fields_node(dict(value.params), check_field, seen=seen)
        return
    if isinstance(value, Unit):
        if value.field is not None:
            check_field(value.field)
        return
    if isinstance(value, Predicate):
        _validate_binding_fields_node(value.left, check_field, seen=seen)
        if value.right is not None:
            _validate_binding_fields_node(value.right, check_field, seen=seen)
        return
    if isinstance(value, GroupExpr):
        if value.predicate is not None:
            _validate_binding_fields_node(value.predicate, check_field, seen=seen)
        if value.members is not None:
            for member in value.members:
                _validate_binding_fields_node(member, check_field, seen=seen)
        if value.left is not None:
            _validate_binding_fields_node(value.left, check_field, seen=seen)
        if value.right is not None:
            _validate_binding_fields_node(value.right, check_field, seen=seen)
        return
    if isinstance(value, Ranking):
        if value.expression is not None:
            _validate_binding_fields_node(value.expression, check_field, seen=seen)
        if value.left is not None:
            _validate_binding_fields_node(value.left, check_field, seen=seen)
        if value.right is not None:
            _validate_binding_fields_node(value.right, check_field, seen=seen)
        return
    if isinstance(value, Entry):
        return
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        try:
            field = _field_from_binding_record(value)
            if field is not None:
                check_field(field)
                return
            for item in value.values():
                _validate_binding_fields_node(item, check_field, seen=seen)
        finally:
            seen.discard(identity)
        return
    if isinstance(value, list | tuple):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        try:
            for item in value:
                _validate_binding_fields_node(item, check_field, seen=seen)
        finally:
            seen.discard(identity)


def _field_from_binding_record(value: Mapping[Any, Any]) -> Field | None:
    """Recognize a Field.to_record() map nested inside Operation params / group records."""

    if "name" not in value or set(value.keys()) - {"name", "kind"}:
        return None
    name = value["name"]
    kind = value.get("kind")
    if not isinstance(name, str) or not name:
        return None
    if kind not in (None, "source", "analysis"):
        return None
    return Field(name, kind)


def canonical_binding_bytes(binding: EncodedBinding) -> bytes:
    """Deterministic UTF-8 representation used for equality."""

    decoded = decode_binding_value(binding.value_kind, binding.value)
    canonical = encode_binding_value(decoded)
    return canonical_json({"value_kind": canonical.value_kind, "value": canonical.value}).encode(
        "utf-8"
    )


def require_namespace_name(name: str) -> str:
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        raise QuailRuntimeError(f"Binding name {name!r} is not a valid identifier")
    if name.startswith("__quail_"):
        raise QuailRuntimeError(f"Binding name {name!r} is reserved")
    if len(name.encode("utf-8")) > MAX_BINDING_NAME_BYTES:
        raise QuailRuntimeError(f"Binding name exceeded its {MAX_BINDING_NAME_BYTES}-byte limit")
    return name


def encode_ordered_literal(value: Any) -> Any:
    return _encode_ordered_literal_node(value)


def decode_ordered_literal(value: Any) -> Any:
    return _decode_ordered_literal_node(value)


def encode_plain_list(
    values: list[Any],
    _active: set[int] | None = None,
    _depth: int = 0,
    _remaining: list[int] | None = None,
) -> dict[str, Any]:
    active = set() if _active is None else _active
    remaining = [MAX_BINDING_ITEMS] if _remaining is None else _remaining
    if _depth >= MAX_BINDING_DEPTH:
        raise QuailRuntimeError(
            f"Ordinary lists exceeded their {MAX_BINDING_DEPTH}-level depth limit"
        )
    remaining[0] -= len(values)
    if remaining[0] < 0:
        raise QuailRuntimeError(f"Ordinary lists cannot exceed {MAX_BINDING_ITEMS} total items")
    identity = id(values)
    if identity in active:
        raise QuailRuntimeError("Cannot persist ordinary lists containing cycles")
    active.add(identity)
    try:
        items = []
        for item in values:
            if type(item) is list:
                encoded = _encoded_binding(
                    "result_list",
                    encode_plain_list(item, active, _depth + 1, remaining),
                )
            else:
                encoded = encode_binding_value(item)
            items.append({"value_kind": encoded.value_kind, "value": encoded.value})
    finally:
        active.remove(identity)
    return {"items": items, "result_kind": "plain_list"}


def decode_result_list(record: Any) -> list[Any]:
    if not isinstance(record, Mapping):
        raise QuailRuntimeError("Result list binding is malformed")
    result_kind = record.get("result_kind")
    items = record.get("items")
    if result_kind != "plain_list" or not isinstance(items, list):
        raise QuailRuntimeError("Result list binding is malformed")
    decoded: list[Any] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise QuailRuntimeError("Result list item is malformed")
        value_kind = item.get("value_kind")
        if not isinstance(value_kind, str):
            raise QuailRuntimeError("Result list item is malformed")
        decoded.append(decode_binding_value(value_kind, item.get("value")))
    return decoded


def binding_to_payload(binding: EncodedBinding) -> dict[str, Any]:
    return {"value_kind": binding.value_kind, "value": binding.value}


def binding_from_payload(payload: Any) -> EncodedBinding:
    if not isinstance(payload, Mapping):
        raise QuailRuntimeError("Binding payload must be an object")
    value_kind = payload.get("value_kind")
    if not isinstance(value_kind, str) or value_kind not in BINDING_KINDS:
        raise QuailRuntimeError("Binding payload value_kind is invalid")
    return EncodedBinding(value_kind, payload.get("value"))  # type: ignore[arg-type]


def bindings_to_payload(bindings: Mapping[str, EncodedBinding]) -> dict[str, Any]:
    return {name: binding_to_payload(binding) for name, binding in bindings.items()}


def bindings_from_payload(payload: Any) -> dict[str, EncodedBinding]:
    if not isinstance(payload, Mapping):
        raise QuailRuntimeError("Bindings payload must be an object")
    return {str(name): binding_from_payload(item) for name, item in payload.items()}


def _encoded_binding(value_kind: BindingKind, value: Any) -> EncodedBinding:
    binding = EncodedBinding(value_kind, value)
    canonical_json({"value_kind": binding.value_kind, "value": binding.value})
    return binding


def _encode_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool | str):
        if isinstance(value, str):
            value.encode("utf-8")
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) >= 10**MAX_INTEGER_DECIMAL_DIGITS:
            raise QuailRuntimeError(
                f"LiteralValue integers cannot exceed {MAX_INTEGER_DECIMAL_DIGITS} decimal digits"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QuailRuntimeError("LiteralValue cannot contain non-finite floats")
        return value
    raise QuailRuntimeError(f"Cannot persist value of type {type(value).__name__}")


def _decode_literal(value: Any) -> Any:
    return _encode_scalar(value) if not isinstance(value, list | dict) else value


def _encode_ordered_literal_node(
    value: Any,
    *,
    _active: set[int] | None = None,
    _depth: int = 0,
    _remaining: list[int] | None = None,
) -> Any:
    active = set() if _active is None else _active
    remaining = [MAX_BINDING_ITEMS] if _remaining is None else _remaining
    if _depth > MAX_BINDING_DEPTH:
        raise QuailRuntimeError(f"LiteralValue exceeded its {MAX_BINDING_DEPTH}-level depth limit")
    remaining[0] -= 1
    if remaining[0] < 0:
        raise QuailRuntimeError(f"LiteralValue exceeded its {MAX_BINDING_ITEMS}-item limit")

    if value is None or isinstance(value, bool | str | int | float):
        return _encode_scalar(value)
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise QuailRuntimeError("LiteralValue cannot contain cycles")
        active.add(identity)
        try:
            return [
                _encode_ordered_literal_node(
                    item,
                    _active=active,
                    _depth=_depth + 1,
                    _remaining=remaining,
                )
                for item in value
            ]
        finally:
            active.remove(identity)
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise QuailRuntimeError("LiteralValue cannot contain cycles")
        active.add(identity)
        try:
            items: list[list[Any]] = []
            for key, item in value.items():
                if not isinstance(key, str):
                    raise QuailRuntimeError("LiteralValue object keys must be strings")
                key.encode("utf-8")
                items.append(
                    [
                        key,
                        _encode_ordered_literal_node(
                            item,
                            _active=active,
                            _depth=_depth + 1,
                            _remaining=remaining,
                        ),
                    ]
                )
            return {_ORDERED_DICT_KEY: items}
        finally:
            active.remove(identity)
    raise QuailRuntimeError(f"LiteralValue does not support {type(value).__name__}")


def _decode_ordered_literal_node(
    value: Any,
    *,
    _depth: int = 0,
    _remaining: list[int] | None = None,
) -> Any:
    remaining = [MAX_BINDING_ITEMS] if _remaining is None else _remaining
    if _depth > MAX_BINDING_DEPTH:
        raise QuailRuntimeError(
            f"Ordered literal exceeded its {MAX_BINDING_DEPTH}-level depth limit"
        )
    remaining[0] -= 1
    if remaining[0] < 0:
        raise QuailRuntimeError(f"Ordered literal exceeded its {MAX_BINDING_ITEMS}-item limit")
    if value is None or isinstance(value, bool | str | int | float):
        return _encode_scalar(value)
    if isinstance(value, list):
        return [
            _decode_ordered_literal_node(item, _depth=_depth + 1, _remaining=remaining)
            for item in value
        ]
    if not isinstance(value, dict) or set(value) != {_ORDERED_DICT_KEY}:
        raise QuailRuntimeError("Ordered dictionary literal is malformed")
    payload = value[_ORDERED_DICT_KEY]
    if not isinstance(payload, list):
        raise QuailRuntimeError("Ordered dictionary items are malformed")
    result: dict[str, Any] = {}
    for pair in payload:
        if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
            raise QuailRuntimeError("Ordered dictionary item is malformed")
        key = pair[0]
        if key in result:
            raise QuailRuntimeError("Ordered dictionary keys must be unique")
        result[key] = _decode_ordered_literal_node(
            pair[1],
            _depth=_depth + 1,
            _remaining=remaining,
        )
    return result


def _decode_unit(record: Any) -> Unit:
    if not isinstance(record, Mapping):
        raise QuailRuntimeError("Unit binding is malformed")
    scope = record.get("scope")
    if scope not in ("entries", "fields", "values"):
        raise QuailRuntimeError("Unit binding is malformed")
    field_record = record.get("field")
    field = None if field_record is None else field_from_record(field_record)
    return Unit(scope, field)


def _decode_ranking(record: Any) -> Ranking:
    if not isinstance(record, Mapping):
        raise QuailRuntimeError("Ranking binding is malformed")
    empty = record.get("empty")
    expression_record = record.get("expression")
    left_record = record.get("left")
    operator = record.get("operator")
    right_record = record.get("right")
    if empty:
        return Ranking()
    if expression_record is not None:
        return Ranking(expression=expression_from_record(expression_record))
    if left_record is None:
        raise QuailRuntimeError("Ranking binding is malformed")
    left = _decode_ranking(left_record)
    if operator == "+":
        if right_record is None:
            raise QuailRuntimeError("Ranking addition requires a right Ranking")
        return Ranking(left=left, operator="+", right=_decode_ranking(right_record))
    if operator == "*":
        return Ranking(left=left, operator="*", right=right_record)
    raise QuailRuntimeError("Ranking binding is malformed")


def _decode_group_expr(record: Any) -> GroupExpr:
    if not isinstance(record, Mapping):
        raise QuailRuntimeError("GroupExpr binding is malformed")
    scope = record.get("scope")
    if scope not in ("entries", "fields"):
        raise QuailRuntimeError("GroupExpr binding is malformed")
    name = record.get("name")
    predicate_record = record.get("predicate")
    members_record = record.get("members")
    operator = record.get("operator")
    left_record = record.get("left")
    right_record = record.get("right")
    if (
        name in ("G0", "G1")
        and predicate_record is None
        and members_record is None
        and operator is None
    ):
        return G0 if name == "G0" else G1
    kwargs: dict[str, Any] = {"scope": scope}
    if name is not None:
        kwargs["name"] = name
    if predicate_record is not None:
        kwargs["predicate"] = predicate_from_record(predicate_record)
    if members_record is not None:
        if not isinstance(members_record, list):
            raise QuailRuntimeError("GroupExpr members record is malformed")
        if scope == "entries":
            kwargs["members"] = [entry_from_record(item) for item in members_record]
        else:
            kwargs["members"] = [field_from_record(item) for item in members_record]
    if operator is not None:
        kwargs["operator"] = operator
        kwargs["left"] = _decode_group_expr(left_record)
        if right_record is not None:
            kwargs["right"] = _decode_group_expr(right_record)
    return GroupExpr(**kwargs)
