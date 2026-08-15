"""Worker-side namespace, print buffer, and host RPC endpoint."""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from quail.analysis.entry import Entry
from quail.analysis.errors import (
    QuailError,
    QuailFieldError,
    QuailRuntimeError,
    QuailScopeError,
    QuailSyntaxError,
    rehydrate_quail_error,
)
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import G0, G1, GroupExpr
from quail.analysis.operations import (
    AsNumber,
    AsText,
    Length,
    Lexical,
    RegexFindAll,
    RegexSearch,
    RegexSub,
    Semantic,
    Slice,
    Value,
)
from quail.analysis.predicate import Predicate
from quail.analysis.ranking import Ranking
from quail.analysis.re_helper import ReFacade
from quail.analysis.unit import Unit, entries, fields
from quail.analysis.worker.protocol import ApiCall, decode_value, encode_api_call

MAX_PRINT_BYTES = 1_048_576

_host_call: contextvars.ContextVar[Callable[[ApiCall], Any] | None] = contextvars.ContextVar(
    "quail_worker_host_call",
    default=None,
)


@dataclass(slots=True)
class PrintBuffer:
    chunks: list[str] = field(default_factory=list)
    byte_count: int = 0

    def write(self, *values: Any, sep: str = " ", end: str = "\n") -> None:
        text = sep.join(str(value) for value in values) + end
        encoded = text.encode("utf-8")
        if self.byte_count + len(encoded) > MAX_PRINT_BYTES:
            raise QuailRuntimeError("print output exceeded the byte limit")
        self.chunks.append(text)
        self.byte_count += len(encoded)

    @property
    def text(self) -> str:
        return "".join(self.chunks)


class HostEndpoint:
    """Issue contiguous ApiCalls to the host and decode results."""

    def __init__(self, send_and_wait: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._send_and_wait = send_and_wait
        self._next_id = 1

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        call = ApiCall(id=self._next_id, method=method, args=args, kwargs=kwargs)
        self._next_id += 1
        response = self._send_and_wait(encode_api_call(call))
        if response.get("type") != "api_result" or int(response.get("id", -1)) != call.id:
            raise QuailRuntimeError("Host returned a mismatched api_result")
        if not response.get("ok"):
            message = str(response.get("message") or "host api_call failed")
            raise rehydrate_quail_error(
                response.get("exception_type"),
                message,
                response.get("repair_hint"),
            )
        return decode_value(response["result"])


def set_host_call(callback: Callable[[ApiCall], Any] | None) -> contextvars.Token[Any]:
    return _host_call.set(callback)


def reset_host_call(token: contextvars.Token[Any]) -> None:
    _host_call.reset(token)


def entry_value_rpc(entry: Entry, field: Field | str, default: Any = None) -> Any:
    callback = _host_call.get()
    if callback is None:
        raise QuailRuntimeError("entry.value() is only available during quail_exec evaluation")
    return callback(
        ApiCall(
            id=0,
            method="entry_value",
            args=(entry, field, default),
            kwargs={},
        )
    )


def entry_fields_rpc(entry: Entry) -> list[Field]:
    callback = _host_call.get()
    if callback is None:
        raise QuailRuntimeError("entry.fields() is only available during quail_exec evaluation")
    result = callback(ApiCall(id=0, method="entry_fields", args=(entry,), kwargs={}))
    if not isinstance(result, list):
        raise QuailRuntimeError("entry.fields() host result must be a list")
    return result


def build_namespace(endpoint: HostEndpoint, prints: PrintBuffer) -> dict[str, Any]:
    """Injected names for one worker exec (no DB imports)."""

    def retrieve(*args: Any, **kwargs: Any) -> Any:
        return endpoint.call("retrieve", *args, **kwargs)

    def count(*args: Any, **kwargs: Any) -> Any:
        return endpoint.call("count", *args, **kwargs)

    def create_field(*args: Any, **kwargs: Any) -> Any:
        return endpoint.call("create_field", *args, **kwargs)

    def tag(*args: Any, **kwargs: Any) -> Any:
        return endpoint.call("tag", *args, **kwargs)

    def untag(*args: Any, **kwargs: Any) -> Any:
        return endpoint.call("untag", *args, **kwargs)

    def quail_print(*values: Any, sep: str = " ", end: str = "\n") -> None:
        prints.write(*values, sep=sep, end=end)

    safe_builtins: dict[str, Any] = {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "bytes": bytes,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "range": range,
        "repr": repr,
        "round": round,
        "set": set,
        "str": str,
        "sum": sum,
        "tuple": tuple,
    }

    return {
        "__builtins__": safe_builtins,
        "retrieve": retrieve,
        "count": count,
        "create_field": create_field,
        "tag": tag,
        "untag": untag,
        "print": quail_print,
        "G0": G0,
        "G1": G1,
        "entries": entries,
        "fields": fields,
        "Field": Field,
        "Unit": Unit,
        "Expression": Expression,
        "Predicate": Predicate,
        "GroupExpr": GroupExpr,
        "Ranking": Ranking,
        "Entry": Entry,
        "Value": Value,
        "AsText": AsText,
        "AsNumber": AsNumber,
        "RegexSearch": RegexSearch,
        "RegexFindAll": RegexFindAll,
        "RegexSub": RegexSub,
        "Slice": Slice,
        "Length": Length,
        "Lexical": Lexical,
        "Semantic": Semantic,
        "re": ReFacade(),
        "QuailError": QuailError,
        "QuailSyntaxError": QuailSyntaxError,
        "QuailScopeError": QuailScopeError,
        "QuailFieldError": QuailFieldError,
        "QuailRuntimeError": QuailRuntimeError,
    }


def host_call_from_endpoint(endpoint: HostEndpoint) -> Callable[[ApiCall], Any]:
    def _call(api_call: ApiCall) -> Any:
        # Entry.value/fields use id=0 placeholders; allocate real ids via endpoint.call.
        return endpoint.call(api_call.method, *api_call.args, **api_call.kwargs)

    return _call
