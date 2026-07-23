"""AST validation for quail_exec worker scripts."""

from __future__ import annotations

import ast

from quail.analysis.errors import QuailSyntaxError

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


def validate_quail_code(code: str) -> ast.Module:
    """Parse and reject unsupported Python constructs for quail_exec."""

    if not isinstance(code, str):
        raise QuailSyntaxError("code must be a string")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as error:
        raise QuailSyntaxError(f"Invalid Python syntax: {error.msg}") from error

    for node in ast.walk(tree):
        if isinstance(node, _REJECTED_NODES):
            raise QuailSyntaxError(f"Unsupported construct in quail_exec: {type(node).__name__}")
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
    return tree
