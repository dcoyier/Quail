"""Expression operations, their declared signatures, and factories."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from quail.analysis.entry import Entry
from quail.analysis.errors import QuailScopeError, QuailSyntaxError
from quail.analysis.literals import literal_as_plain, seal_mapping

from quail.analysis.re_helper import require_regex_text, validate_regex_flags
from quail.analysis.regex_engine import compile_regex


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


# Pipeline kinds and op signatures. See docs/core.md: a pipeline is legal iff
# each op accepts the kind the previous op produced; "score" is terminal.
# "any" is an unread field value; "text_or_list" is text or list[text] proven
# at runtime by a preceding op.
_KIND_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "any": "the raw field value",
        "text": "text",
        "number": "a number",
        "list_text": "list[text]",
        "text_or_list": "text or list[text]",
        "score": "a score",
    }
)

_TEXT = frozenset({"any", "text", "text_or_list"})
_TEXT_OR_LIST = frozenset({"any", "text", "list_text", "text_or_list"})
_ANY_VALUE = frozenset({"any", "text", "number", "list_text", "text_or_list"})


@dataclass(frozen=True, slots=True)
class OpSpec:
    """Declared signature of one op: what it accepts and what it produces.

    ``produces`` is a kind, ``"same"`` (pass the incoming kind through), or
    ``"narrow"`` (pass it through, but "any" becomes "text_or_list" because
    the op proves textuality at runtime).
    """

    accepts: frozenset[str]
    produces: str
    needs: str
    terminal: bool = False
    first_only: bool = False


OP_SPECS: Mapping[str, OpSpec] = MappingProxyType(
    {
        "Value": OpSpec(
            accepts=frozenset({"any"}),
            produces="same",
            needs="the raw field value",
            first_only=True,
        ),
        "AsText": OpSpec(accepts=_ANY_VALUE, produces="text", needs="any value"),
        "AsNumber": OpSpec(
            accepts=frozenset({"any", "text", "number", "text_or_list"}),
            produces="number",
            needs="a number or numeric text",
        ),
        "RegexSearch": OpSpec(accepts=_TEXT, produces="text", needs="text"),
        "RegexFindAll": OpSpec(accepts=_TEXT, produces="list_text", needs="text"),
        "RegexSub": OpSpec(accepts=_TEXT_OR_LIST, produces="narrow", needs="text or list[text]"),
        "Slice": OpSpec(accepts=_TEXT_OR_LIST, produces="narrow", needs="text or list[text]"),
        "Length": OpSpec(accepts=_TEXT_OR_LIST, produces="number", needs="text or list[text]"),
        "Lexical": OpSpec(
            accepts=_TEXT_OR_LIST,
            produces="score",
            needs="text or list[text]",
            terminal=True,
        ),
        "Semantic": OpSpec(
            accepts=_TEXT_OR_LIST,
            produces="score",
            needs="text or list[text]",
            terminal=True,
        ),
    }
)


def validate_operation_pipeline(operations: tuple[Operation, ...]) -> None:
    final_pipeline_kind(operations)


def final_pipeline_kind(operations: tuple[Operation, ...]) -> str:
    """Walk the pipeline through OP_SPECS; give back the kind it produces."""

    if not operations:
        raise QuailSyntaxError("Expression requires at least one operation")
    current = "any"
    last_index = len(operations) - 1
    for index, operation in enumerate(operations):
        kind = operation.kind
        spec = OP_SPECS.get(kind)
        if spec is None:
            raise QuailSyntaxError(f"Unsupported operation: {kind}")
        if spec.first_only and index != 0:
            raise QuailSyntaxError(f"{kind}() is valid only as the first operation")
        if current not in spec.accepts:
            raise QuailSyntaxError(
                f"{kind}(...) needs {spec.needs}; the pipeline here produces "
                f"{_KIND_LABELS[current]}"
            )
        if spec.terminal and index != last_index:
            raise QuailSyntaxError(
                "Lexical(...) and Semantic(...) must end the expression pipeline"
            )
        if spec.produces == "narrow":
            current = "text_or_list" if current == "any" else current
        elif spec.produces != "same":
            current = spec.produces
    return current


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
    if kind == "RegexSub":
        replacement = params.get("replacement")
        if not isinstance(replacement, str):
            raise QuailSyntaxError("RegexSub replacement must be a string")
        require_regex_text(replacement, "RegexSub replacement")
    compile_regex(pattern, flags)
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
