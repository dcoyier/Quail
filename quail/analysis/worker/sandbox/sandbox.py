"""AST validation for quail_exec worker scripts."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from quail.analysis.bindings import RESERVED_NAMES, require_namespace_name
from quail.analysis.errors import QuailRuntimeError, QuailSyntaxError

_REJECTED_NODES: tuple[type, ...] = (
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Raise,
    ast.Assert,
    ast.Match,
    ast.Await,
    ast.Yield,
    ast.YieldFrom,
    ast.Global,
    ast.Nonlocal,
    ast.JoinedStr,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.NamedExpr,
    ast.AnnAssign,
    ast.AugAssign,
    ast.AsyncFor,
)
if hasattr(ast, "TryStar"):
    _REJECTED_NODES = (*_REJECTED_NODES, ast.TryStar)

_REJECTED_NODE_ENGLISH: dict[type, str] = {
    ast.Import: "import",
    ast.ImportFrom: "import",
    ast.FunctionDef: "def",
    ast.AsyncFunctionDef: "async def",
    ast.ClassDef: "class",
    ast.Lambda: "lambda",
    ast.With: "with",
    ast.AsyncWith: "async with",
    ast.Try: "try/except",
    ast.Raise: "raise",
    ast.Assert: "assert",
    ast.Match: "match",
    ast.Await: "await",
    ast.Yield: "yield",
    ast.YieldFrom: "yield from",
    ast.Global: "global",
    ast.Nonlocal: "nonlocal",
    ast.JoinedStr: "f-strings",
    ast.ListComp: "list comprehensions",
    ast.SetComp: "set comprehensions",
    ast.DictComp: "dict comprehensions",
    ast.GeneratorExp: "generator expressions",
    ast.NamedExpr: "walrus :=",
    ast.AnnAssign: "annotated assignment",
    ast.AugAssign: "augmented assignment",
    ast.AsyncFor: "async for",
}
if hasattr(ast, "TryStar"):
    _REJECTED_NODE_ENGLISH[ast.TryStar] = "try/except*"

_DANGEROUS_NAMES = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "memoryview",
        "help",
    }
)

APPROVED_STRING_METHODS = frozenset(
    {
        "startswith",
        "endswith",
        "lower",
        "upper",
        "casefold",
        "strip",
        "lstrip",
        "rstrip",
        "replace",
        "split",
        "rsplit",
        "splitlines",
        "count",
        "find",
        "rfind",
        "removeprefix",
        "removesuffix",
    }
)

APPROVED_QUAIL_METHODS = frozenset({"value", "fields", "where", "escape"})
APPROVED_ATTRIBUTE_CALLS = APPROVED_STRING_METHODS | APPROVED_QUAIL_METHODS

CONTAINER_MUTATION_METHODS = frozenset(
    {
        "add",
        "append",
        "clear",
        "discard",
        "extend",
        "insert",
        "pop",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "update",
    }
)

APPROVED_DATA_ATTRIBUTES = frozenset(
    {
        "name",
        "kind",
        "scope",
        "field",
        "input",
        "operations",
        "pattern",
        "flags",
        "replacement",
        "start",
        "end",
        "query",
        "input_aggregation",
        "target_aggregation",
        "expression",
        "left",
        "operator",
        "right",
        "predicate",
        "members",
        "id",
        "dataset_id",
        "dataset_version_id",
        "dataset",
        "A",
        "ASCII",
        "I",
        "IGNORECASE",
        "M",
        "MULTILINE",
        "NOFLAG",
        "S",
        "DOTALL",
        "U",
        "UNICODE",
    }
)


@dataclass(frozen=True, slots=True)
class ValidatedQuailProgram:
    tree: ast.Module
    assigned_names: frozenset[str]
    deleted_names: frozenset[str]


def validate_quail_code(code: str) -> ValidatedQuailProgram:
    """Parse and reject unsupported Python constructs for quail_exec."""

    if not isinstance(code, str):
        raise QuailSyntaxError("code must be a string")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as error:
        raise QuailSyntaxError(f"Invalid Python syntax: {error.msg}") from error

    assigned: set[str] = set()
    deleted: set[str] = set()
    call_funcs = {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    for node in ast.walk(tree):
        if isinstance(node, _REJECTED_NODES):
            label = _REJECTED_NODE_ENGLISH.get(type(node), type(node).__name__)
            raise QuailSyntaxError(f"Unsupported construct in quail_exec: {label}")
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.Is | ast.IsNot) for op in node.ops
        ):
            raise QuailSyntaxError("Use == / != instead of is / is not")
        if isinstance(node, ast.Name) and node.id in _DANGEROUS_NAMES:
            raise QuailSyntaxError(f"Name {node.id!r} is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise QuailSyntaxError("Private attributes are not allowed")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Entry"
        ):
            raise QuailSyntaxError("Entry handles are created by Quail, not user code")
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store | ast.Del):
            raise QuailSyntaxError("Item assignment and deletion are not allowed")
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store | ast.Del):
            raise QuailSyntaxError("Attribute assignment and deletion are not allowed")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            _check_binding_name(node.id)
            assigned.add(node.id)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Del):
            _check_binding_name(node.id)
            deleted.add(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            _check_attribute_load(node, is_call_func=id(node) in call_funcs)
        if isinstance(node, ast.Call):
            _check_call(node)

    return ValidatedQuailProgram(
        tree=tree,
        assigned_names=frozenset(assigned),
        deleted_names=frozenset(deleted),
    )


def _check_binding_name(name: str) -> None:
    if name in RESERVED_NAMES:
        raise QuailSyntaxError(f"Cannot assign or delete reserved name {name!r}")
    try:
        require_namespace_name(name)
    except QuailRuntimeError as error:
        raise QuailSyntaxError(str(error)) from error


def _check_attribute_load(node: ast.Attribute, *, is_call_func: bool) -> None:
    if node.attr not in APPROVED_DATA_ATTRIBUTES | APPROVED_ATTRIBUTE_CALLS:
        raise QuailSyntaxError(_public_attribute_error(node))
    if node.attr in APPROVED_ATTRIBUTE_CALLS and not is_call_func:
        signatures = {
            "value": "entry.value(field, default=None)",
            "fields": "entry.fields()",
            "where": "group.where(predicate)",
            "escape": "re.escape(pattern)",
        }
        signature = signatures.get(node.attr, f"text.{node.attr}(...)")
        raise QuailSyntaxError(
            f"Method {node.attr!r} must be called as {signature}; "
            "approved methods must be called directly on their receiver"
        )


def _check_call(node: ast.Call) -> None:
    if isinstance(node.func, ast.Name) and node.func.id in {"entries", "fields"}:
        raise QuailSyntaxError(
            f"{node.func.id} is a result unit, not a function; pass unit={node.func.id} "
            "to retrieve() or count()"
        )
    if not isinstance(node.func, ast.Attribute):
        return
    attr = node.func.attr
    if attr in APPROVED_ATTRIBUTE_CALLS:
        return
    if attr in APPROVED_DATA_ATTRIBUTES:
        raise QuailSyntaxError(
            f"Attribute {attr!r} is data, not a method; use .{attr} without parentheses"
        )
    if attr in {"retrieve", "count"}:
        raise QuailSyntaxError(
            f"{attr}(...) is a top-level Quail function; pass the group with {attr}(group=...)"
        )
    if attr == "get":
        raise QuailSyntaxError(
            ".get(...) is unavailable; subscript a local dict with mapping[key], "
            "or read an Entry field with entry.value(field)"
        )
    if attr == "compile" and isinstance(node.func.value, ast.Name) and node.func.value.id == "re":
        raise QuailSyntaxError(
            "re.compile(...) is unavailable; use RegexSearch, RegexFindAll, or "
            "RegexSub inside an Expression"
        )
    if attr in {"items", "keys", "values"}:
        raise QuailSyntaxError(
            f"Dictionary .{attr}(...) is unavailable; iterate the local dict directly"
        )
    if attr in CONTAINER_MUTATION_METHODS:
        raise QuailSyntaxError(
            f"Container mutation .{attr}(...) is unavailable; "
            "rebuild and rebind a persistable list or dict"
        )
    raise QuailSyntaxError(f"Method {attr!r} is not available in quail_exec")


def _public_attribute_error(node: ast.Attribute) -> str:
    receiver = node.value
    if isinstance(receiver, ast.Name) and receiver.id in {"G0", "G1"}:
        return (
            f"Group attribute {node.attr!r} is not public; filter entry groups with "
            "G0.where(predicate) and materialize groups with retrieve(...) or count(...)"
        )
    return f"Attribute {node.attr!r} is not available in quail_exec"
