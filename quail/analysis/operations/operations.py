"""Expression operations and factories."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from quail.analysis.entry import Entry
from quail.analysis.errors import QuailScopeError, QuailSyntaxError
from quail.analysis.literals import literal_as_plain, seal_mapping

from quail.analysis.re_helper import require_regex_text, validate_regex_flags


@dataclass(frozen=True, slots=True)
class Operation:
    kind: str
    params: MappingProxyType[str, Any]

    def __init__(self, kind: str, params: dict[str, Any] | None = None) -> None:
        if not isinstance(kind, str) or not kind:
            raise QuailSyntaxError("Operation kind must be a non-empty string")
        if params is not None and not isinstance(params, dict):
            raise QuailSyntaxError("Operation params must be a dict or None")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "params", seal_mapping({} if params is None else params))

    def to_record(self) -> dict[str, Any]:
        return {"kind": self.kind, **literal_as_plain(self.params)}

    def __getattr__(self, name: str) -> Any:
        if self.kind == "Slice" and name == "end":
            return literal_as_plain(self.params["end"])
        try:
            return literal_as_plain(self.params[name])
        except KeyError as error:
            raise AttributeError(f"{self.kind} operation has no attribute {name!r}") from error


def validate_operation_pipeline(operations: tuple[Operation, ...]) -> None:
    if not operations:
        raise QuailSyntaxError("Expression requires at least one operation")
    current_type = "any"
    for index, operation in enumerate(operations):
        kind = operation.kind
        if kind == "Value":
            if index != 0:
                raise QuailSyntaxError("Value() is valid only as the first operation")
            continue
        if kind == "AsText":
            current_type = "text"
            continue
        if kind == "AsNumber":
            current_type = "number"
            continue
        if kind in ("RegexSearch", "RegexFindAll"):
            if current_type not in ("any", "text", "text_or_list"):
                raise QuailSyntaxError(f"{kind} requires a text expression")
            current_type = "list_text" if kind == "RegexFindAll" else "text"
            continue
        if kind in ("RegexSub", "Slice"):
            if current_type not in ("any", "text", "list_text", "text_or_list"):
                raise QuailSyntaxError(f"{kind} requires a text or list[text] expression")
            if current_type == "any":
                current_type = "text_or_list"
            continue
        if kind == "Length":
            if current_type not in ("any", "text", "list_text", "text_or_list"):
                raise QuailSyntaxError("Length cannot consume a numeric expression")
            current_type = "number"
            continue
        if kind in ("Lexical", "Semantic"):
            if current_type not in ("any", "text", "list_text", "text_or_list"):
                raise QuailSyntaxError(f"{kind} cannot consume a numeric expression")
            if index != len(operations) - 1:
                raise QuailSyntaxError(
                    "Lexical(...) and Semantic(...) must end the expression pipeline"
                )
            current_type = "score"
            continue
        raise QuailSyntaxError(f"Unsupported operation: {kind}")


def Value() -> Operation:
    return Operation("Value")


def AsText() -> Operation:
    return Operation("AsText")


def AsNumber() -> Operation:
    return Operation("AsNumber")


def RegexSearch(pattern: str, flags: int = 0) -> Operation:
    return _regex_operation("RegexSearch", pattern, flags=flags)


def RegexFindAll(pattern: str, flags: int = 0) -> Operation:
    return _regex_operation("RegexFindAll", pattern, flags=flags)


def RegexSub(pattern: str, replacement: str, flags: int = 0) -> Operation:
    if not isinstance(replacement, str):
        raise QuailSyntaxError("RegexSub replacement must be a string")
    require_regex_text(replacement, "RegexSub replacement")
    return _regex_operation("RegexSub", pattern, flags=flags, replacement=replacement)


def Slice(start: int, end: int | None = None) -> Operation:
    if isinstance(start, bool) or not isinstance(start, int):
        raise QuailSyntaxError("Slice start must be an int")
    if end is not None and (isinstance(end, bool) or not isinstance(end, int)):
        raise QuailSyntaxError("Slice end must be an int or None")
    return Operation("Slice", {"start": start, "end": end})


def Length() -> Operation:
    return Operation("Length")


def Lexical(
    query: Any,
    input_aggregation: str | None = None,
    target_aggregation: str | None = None,
) -> Operation:
    return _similarity_operation(
        "Lexical",
        query,
        input_aggregation=input_aggregation,
        target_aggregation=target_aggregation,
    )


def Semantic(
    query: Any,
    input_aggregation: str | None = None,
    target_aggregation: str | None = None,
) -> Operation:
    return _similarity_operation(
        "Semantic",
        query,
        input_aggregation=input_aggregation,
        target_aggregation=target_aggregation,
    )


def _regex_operation(kind: str, pattern: str, *, flags: int = 0, **params: Any) -> Operation:
    require_regex_text(pattern, "Regex pattern")
    validate_regex_flags(flags)
    # Full RE2 compile validation lands with the regex engine; shape is checked now.
    return Operation(kind, {"pattern": pattern, "flags": flags, **params})


def _similarity_operation(
    kind: str,
    query: Any,
    *,
    input_aggregation: str | None,
    target_aggregation: str | None,
) -> Operation:
    for key, value in (
        ("input_aggregation", input_aggregation),
        ("target_aggregation", target_aggregation),
    ):
        if value not in (None, "total", "avg"):
            raise QuailSyntaxError(f'{kind} {key} must be "total", "avg", or None')
    return Operation(
        kind,
        {
            "query": _similarity_query_record(kind, query),
            "input_aggregation": input_aggregation,
            "target_aggregation": target_aggregation,
        },
    )


def _similarity_query_record(operation_kind: str, query: Any) -> dict[str, Any]:
    # Late import avoids cycles with group.py
    from quail.analysis.group import GroupExpr

    if isinstance(query, str):
        if not query:
            raise QuailSyntaxError(
                f"{operation_kind} query must contain at least one non-empty text target"
            )
        return {"kind": "LiteralText", "text": query}
    if isinstance(query, GroupExpr):
        if query.scope != "entries":
            raise QuailScopeError(f"{operation_kind} target groups must be entry-scoped")
        return {"kind": "EntryGroup", "group": query.to_record()}
    if isinstance(query, list):
        if all(isinstance(item, str) for item in query):
            if not any(query):
                raise QuailSyntaxError(
                    f"{operation_kind} query must contain at least one non-empty text target"
                )
            return {"kind": "LiteralTextList", "texts": list(query)}
        if all(isinstance(item, Entry) for item in query):
            if not query:
                raise QuailSyntaxError(
                    f"{operation_kind} query must contain at least one Entry target"
                )
            return {
                "kind": "EntryList",
                "entries": [entry.to_record() for entry in query],
            }
    raise QuailSyntaxError(
        f"{operation_kind} query must be text, list[str], an entry group, or list[Entry]"
    )
