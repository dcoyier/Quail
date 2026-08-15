"""NDJSON wire types and encode/decode for worker RPC."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from quail.analysis.entry import Entry, make_entry
from quail.analysis.errors import QuailSyntaxError
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import G0, G1, GroupExpr
from quail.analysis.operations import Operation
from quail.analysis.predicate import Predicate
from quail.analysis.ranking import Ranking
from quail.analysis.unit import Unit

PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class ApiCall:
    id: int
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    line: int | None = None


def encode_value(value: Any) -> dict[str, Any]:
    if value is None or isinstance(value, bool | int | float | str):
        return {"kind": "literal", "value": value}
    if isinstance(value, Field):
        encoded = {"kind": "Field", "name": value.name, "field_kind": value.kind}
        if value.bound_dataset_id:
            encoded["bound_dataset_id"] = value.bound_dataset_id
        return encoded
    if isinstance(value, Entry):
        return {
            "kind": "Entry",
            "id": value.id,
            "dataset_id": value.dataset_id,
            "dataset_version_id": value.dataset_version_id,
            "dataset": value.dataset,
        }
    if isinstance(value, Unit):
        return {
            "kind": "Unit",
            "scope": value.scope,
            "field": None if value.field is None else encode_value(value.field),
        }
    if isinstance(value, Operation):
        return {
            "kind": "Operation",
            "op_kind": value.kind,
            "params": _encode_plain(dict(value.params)),
        }
    if isinstance(value, Expression):
        return {
            "kind": "Expression",
            "root": encode_value(value.root),
            "operations": [encode_value(op) for op in value.operations],
        }
    if isinstance(value, Predicate):
        encoded: dict[str, Any] = {
            "kind": "Predicate",
            "operator": value.operator,
            "left": encode_value(value.left),
        }
        if value.operator != "not":
            encoded["right"] = encode_value(value.right)
        return encoded
    if isinstance(value, GroupExpr):
        encoded = {
            "kind": "GroupExpr",
            "scope": value.scope,
            "name": value.name,
            "predicate": None if value.predicate is None else encode_value(value.predicate),
            "members": None
            if value.members is None
            else [encode_value(member) for member in value.members],
            "left": None if value.left is None else encode_value(value.left),
            "operator": value.operator,
            "right": None if value.right is None else encode_value(value.right),
        }
        if value.bound_dataset_id:
            encoded["bound_dataset_id"] = value.bound_dataset_id
        return encoded
    if isinstance(value, Ranking):
        return {
            "kind": "Ranking",
            "expression": None if value.expression is None else encode_value(value.expression),
            "left": None if value.left is None else encode_value(value.left),
            "operator": value.operator,
            "right": None if value.right is None else encode_value(value.right),
        }
    if isinstance(value, list):
        return {"kind": "list", "items": [encode_value(item) for item in value]}
    if isinstance(value, tuple):
        return {"kind": "tuple", "items": [encode_value(item) for item in value]}
    if isinstance(value, dict):
        return {
            "kind": "dict",
            "items": [[key, encode_value(item)] for key, item in value.items()],
        }
    raise QuailSyntaxError(f"Cannot encode value of type {type(value).__name__}")


def decode_value(payload: dict[str, Any]) -> Any:
    kind = payload.get("kind")
    if kind == "literal":
        return payload.get("value")
    if kind == "Field":
        return Field(
            payload["name"],
            kind=payload.get("field_kind"),
            bound_dataset_id=payload.get("bound_dataset_id"),
        )
    if kind == "Entry":
        return make_entry(
            payload["id"],
            dataset_id=payload.get("dataset_id", ""),
            dataset_version_id=payload.get("dataset_version_id", ""),
            dataset=payload.get("dataset") or "",
        )
    if kind == "Unit":
        field_payload = payload.get("field")
        field = None if field_payload is None else decode_value(field_payload)
        return Unit(scope=payload["scope"], field=field)
    if kind == "Operation":
        return Operation(kind=payload["op_kind"], params=dict(payload.get("params") or {}))
    if kind == "Expression":
        root = decode_value(payload["root"])
        operations = [decode_value(item) for item in payload.get("operations") or []]
        return Expression(root, *operations)
    if kind == "Predicate":
        left = decode_value(payload["left"])
        operator = payload["operator"]
        if operator == "not":
            return Predicate(left, "not")
        return Predicate(left, operator, decode_value(payload["right"]))
    if kind == "GroupExpr":
        name = payload.get("name")
        if name == "G0":
            base = G0
        elif name == "G1":
            base = G1
        else:
            base = None
        if (
            base is not None
            and payload.get("operator") is None
            and payload.get("predicate") is None
            and payload.get("members") is None
            and not payload.get("bound_dataset_id")
        ):
            return base
        kwargs: dict[str, Any] = {"scope": payload["scope"]}
        if name is not None:
            kwargs["name"] = name
        if payload.get("predicate") is not None:
            kwargs["predicate"] = decode_value(payload["predicate"])
        if payload.get("members") is not None:
            kwargs["members"] = [decode_value(item) for item in payload["members"]]
        if payload.get("operator") is not None:
            kwargs["left"] = decode_value(payload["left"])
            kwargs["operator"] = payload["operator"]
            if payload.get("right") is not None:
                kwargs["right"] = decode_value(payload["right"])
        bound_dataset_id = payload.get("bound_dataset_id")
        if isinstance(bound_dataset_id, str) and bound_dataset_id:
            kwargs["bound_dataset_id"] = bound_dataset_id
        return GroupExpr(**kwargs)
    if kind == "Ranking":
        if payload.get("expression") is not None:
            return Ranking(expression=decode_value(payload["expression"]))
        if payload.get("operator") is not None:
            return Ranking(
                left=decode_value(payload["left"]),
                operator=payload["operator"],
                right=decode_value(payload["right"]),
            )
        return Ranking()
    if kind == "list":
        return [decode_value(item) for item in payload.get("items") or []]
    if kind == "tuple":
        return tuple(decode_value(item) for item in payload.get("items") or [])
    if kind == "dict":
        return {key: decode_value(item) for key, item in payload.get("items") or []}
    raise QuailSyntaxError(f"Cannot decode wire kind {kind!r}")


def dumps_message(message: dict[str, Any]) -> str:
    payload = {"version": PROTOCOL_VERSION, **message}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def loads_message(line: str) -> dict[str, Any]:
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise QuailSyntaxError("Protocol message must be an object")
    if int(payload.get("version", 0)) != PROTOCOL_VERSION:
        raise QuailSyntaxError("Unsupported protocol version")
    return payload


def encode_api_call(call: ApiCall) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "api_call",
        "id": call.id,
        "method": call.method,
        "args": [encode_value(arg) for arg in call.args],
        "kwargs": {key: encode_value(value) for key, value in call.kwargs.items()},
    }
    if call.line is not None:
        message["line"] = call.line
    return message


def decode_api_call(message: dict[str, Any]) -> ApiCall:
    return ApiCall(
        id=int(message["id"]),
        method=str(message["method"]),
        args=tuple(decode_value(item) for item in message.get("args") or []),
        kwargs={key: decode_value(value) for key, value in (message.get("kwargs") or {}).items()},
        line=None if message.get("line") is None else int(message["line"]),
    )


def _encode_plain(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_encode_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _encode_plain(item) for key, item in value.items()}
    raise QuailSyntaxError(f"Cannot encode operation param {type(value).__name__}")
