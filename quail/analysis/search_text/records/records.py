"""Rebuild GroupExpr / Entry / Expression / Predicate from to_record payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from quail.analysis.entry import Entry, make_entry
from quail.analysis.errors import QuailRuntimeError, QuailSyntaxError
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import G0, G1, GroupExpr
from quail.analysis.literals import seal_literal
from quail.analysis.operations import Operation
from quail.analysis.predicate import Predicate


def group_expr_from_record(record: Any) -> GroupExpr:
    """Rebuild an entry/field GroupExpr from GroupExpr.to_record() output."""

    if not isinstance(record, Mapping):
        raise QuailRuntimeError("EntryGroup query group record is malformed")
    scope = record.get("scope")
    if scope not in ("entries", "fields"):
        raise QuailRuntimeError("EntryGroup query group record is malformed")
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
        if not isinstance(members_record, Sequence) or isinstance(members_record, str | bytes):
            raise QuailRuntimeError("EntryGroup members record is malformed")
        kwargs["members"] = [entry_from_record(item) for item in members_record]
    if operator is not None:
        kwargs["operator"] = operator
        kwargs["left"] = group_expr_from_record(left_record)
        if right_record is not None:
            kwargs["right"] = group_expr_from_record(right_record)
    return GroupExpr(**kwargs)


def entry_from_record(record: Any) -> Entry:
    """Rebuild an Entry handle from Entry.to_record() output."""

    if not isinstance(record, Mapping):
        raise QuailRuntimeError("EntryList entry record is malformed")
    entry_id = record.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        raise QuailRuntimeError("EntryList entry record is malformed")
    return make_entry(
        entry_id,
        dataset_id=str(record.get("dataset_id") or ""),
        dataset_version_id=str(record.get("dataset_version_id") or ""),
        dataset=str(record.get("dataset") or ""),
    )


def predicate_from_record(record: Any) -> Predicate:
    if not isinstance(record, Mapping) or record.get("kind") != "Predicate":
        raise QuailRuntimeError("Predicate record is malformed")
    operator = record.get("operator")
    if not isinstance(operator, str):
        raise QuailRuntimeError("Predicate record is malformed")
    left = _operand_from_record(record.get("left"))
    if operator == "not":
        return Predicate(left, "not")
    return Predicate(left, operator, _operand_from_record(record.get("right")))


def expression_from_record(record: Any) -> Expression:
    if not isinstance(record, Mapping) or record.get("kind") != "Expression":
        raise QuailRuntimeError("Expression record is malformed")
    root = field_from_record(record.get("root"))
    operations_record = record.get("operations") or []
    if not isinstance(operations_record, Sequence) or isinstance(operations_record, str | bytes):
        raise QuailRuntimeError("Expression record is malformed")
    operations = [operation_from_record(item) for item in operations_record]
    return Expression(root, *operations)


def field_from_record(record: Any) -> Field:
    if not isinstance(record, Mapping):
        raise QuailSyntaxError("Field record is malformed")
    name = record.get("name")
    kind = record.get("kind")
    if not isinstance(name, str) or not name:
        raise QuailSyntaxError("Field record is malformed")
    return Field(name, kind=kind)


def operation_from_record(record: Any) -> Operation:
    if not isinstance(record, Mapping):
        raise QuailRuntimeError("Operation record is malformed")
    # Operation.to_record flattens params beside kind.
    kind = record.get("kind")
    if not isinstance(kind, str) or not kind:
        raise QuailRuntimeError("Operation record is malformed")
    params = {key: value for key, value in record.items() if key != "kind"}
    return Operation(kind, params)


def _operand_from_record(value: Any) -> Any:
    if isinstance(value, Mapping) and value.get("kind") == "Expression":
        return expression_from_record(value)
    if isinstance(value, Mapping) and value.get("kind") == "Predicate":
        return predicate_from_record(value)
    return seal_literal(value)
