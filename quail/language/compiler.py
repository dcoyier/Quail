"""One inspectable expression-to-SQL compiler, shared by every projection.

Named bindings let reused fragments appear in SELECT, filters, and ordering
without duplicating parameter bookkeeping. Joins are reused by field/search
identity, and every scalar fragment retains its decoding contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from quail.contracts import decode_json, sql_identifier
from quail.language.expressions import Kind, Node
from quail.language.scalars import Encoding, SQLValue

ENCODINGS: dict[Kind, Encoding] = {
    "any": "json",
    "text": "text",
    "number": "number",
    "list": "json",
    "predicate": "bool",
}
COMPARISONS = {"eq": "=", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}


@dataclass(frozen=True)
class Fragment:
    sql: str
    encoding: Encoding
    kind: Kind


@dataclass(frozen=True)
class Query:
    sql: str
    parameters: dict[str, SQLValue]
    projections: tuple[Fragment, ...]
    dependencies: frozenset[str]


class Compiler:
    def __init__(self, source_fields: tuple[str, ...], scores: Mapping[Node, str]) -> None:
        self.source_fields = source_fields
        self.scores = scores
        self.parameters: dict[str, SQLValue] = {}
        self.joins: dict[Node, str] = {}
        self.fragments: dict[Node, Fragment] = {}

    def bind(self, value: SQLValue) -> str:
        name = f"p{len(self.parameters)}"
        self.parameters[name] = value
        return ":" + name

    def compile(self, node: Node) -> Fragment:
        if node in self.fragments:
            return self.fragments[node]
        encoding = ENCODINGS[node.kind]
        if node.op == "literal":
            result = Fragment(self.bind(node.data[0]), "json", node.kind)
        elif node.op == "field":
            name = str(node.data[0])
            if name in self.source_fields:
                result = Fragment("e." + sql_identifier(name), "text", node.kind)
            else:
                alias = f"t{len(self.joins)}"
                self.joins[node] = (
                    f"LEFT JOIN temp.working_tags AS {alias} ON {alias}.entry=e.id "
                    f"AND {alias}.field={self.bind(name)}"
                )
                result = Fragment(f"{alias}.value", "json", node.kind)
        elif node.op in {"lexical", "semantic"}:
            alias = f"s{len(self.joins)}"
            self.joins[node] = (
                f"LEFT JOIN temp.{sql_identifier(self.scores[node])} AS {alias} "
                f"ON {alias}.rowid=e.rowid"
            )
            result = Fragment(f"{alias}.score", "number", "number")
        elif node.op == "random":
            result = Fragment(f"q_random({self.bind(node.data[0])}, e.id)", "number", "number")
        else:
            args = [self.compile(arg) for arg in node.args]
            if node.op in {"and", "or"}:
                sql = f"({args[0].sql} {node.op.upper()} {args[1].sql})"
            elif node.op == "not":
                sql = f"(NOT {args[0].sql})"
            elif node.op in COMPARISONS:
                sql = self._comparison(node, args)
            elif node.op in {"add", "subtract", "multiply", "divide"}:
                parts = [
                    self.bind(node.op),
                    args[0].sql,
                    self.bind(args[0].encoding),
                    args[1].sql,
                    self.bind(args[1].encoding),
                ]
                sql = f"q_arithmetic({', '.join(parts)})"
            else:
                parts = [
                    self.bind(node.op),
                    args[0].sql,
                    self.bind(args[0].encoding),
                    self.bind(encoding),
                    *(self.bind(item) for item in node.data),
                ]
                sql = f"q_value({', '.join(parts)})"
            result = Fragment(sql, encoding, node.kind)
        self.fragments[node] = result
        return result

    def _native(self, node: Node, fragment: Fragment) -> tuple[str, str] | None:
        if fragment.encoding in {"text", "number", "bool"}:
            return fragment.sql, "text" if fragment.encoding == "text" else "number"
        if node.op == "literal":
            value = decode_json(str(node.data[0]))
            if isinstance(value, str):
                return self.bind(value), "text"
            if isinstance(value, float) or isinstance(value, int) and -(2**63) <= value < 2**63:
                return self.bind(value), "number"
        return None

    def _comparison(self, node: Node, args: list[Fragment]) -> str:
        side = 1 if node.args[0].op == "literal" else (2 if node.args[1].op == "literal" else 0)
        if side and node.args[side - 1].data[0] == "null":
            other = args[1 if side == 1 else 0].sql
            if node.op in {"eq", "ne"}:
                return f"({other} IS {'NOT ' if node.op == 'ne' else ''}NULL)"
            return "0"
        left, right = (
            self._native(arg, fragment) for arg, fragment in zip(node.args, args, strict=True)
        )
        if left is not None and right is not None and left[1] == right[1]:
            a, b = left[0], right[0]
            # Preserve a boolean result on blanks while exposing ordinary indexed
            # comparisons to SQLite. Hiding id equality inside a UDF made an Entry
            # expression read scan the corpus; numeric score filters paid needless
            # per-row JSON decoding as well.
            return f"({a} IS NOT NULL AND {b} IS NOT NULL AND {a} {COMPARISONS[node.op]} {b})"
        parts = [
            self.bind(node.op),
            args[0].sql,
            self.bind(args[0].encoding),
            args[1].sql,
            self.bind(args[1].encoding),
            self.bind(side),
        ]
        return f"q_compare({', '.join(parts)})"

    def select(
        self,
        projections: Sequence[Node],
        *,
        where: Node | None = None,
        rank: Node | None = None,
        limit: int | None = None,
        offset: int = 0,
        aggregate: bool = False,
        identity: bool = False,
        restrict_ids: bool = False,
        as_json: bool = False,
    ) -> Query:
        fragments = tuple(self.compile(node) for node in projections)
        columns = [part.sql for part in fragments]
        if as_json:
            columns = [f"q_json({part.sql}, {self.bind(part.encoding)})" for part in fragments]
        if identity:
            columns = ["e.rowid", "e.id", *columns]
        if aggregate:
            columns = ["count(*)"]
        conditions = [self.compile(where).sql] if where is not None else []
        if restrict_ids:
            conditions.append("e.id IN (SELECT entry FROM temp.tag_targets)")
        ordering = "e.rowid"
        if rank is not None:
            score = self.compile(rank).sql
            ordering = f"{score} DESC NULLS LAST, e.rowid"
        sql = "SELECT " + ", ".join(columns) + " FROM main.entries AS e "
        sql += " ".join(self.joins.values())
        if conditions:
            sql += " WHERE " + " AND ".join(f"({part})" for part in conditions)
        if not aggregate:
            sql += " ORDER BY " + ordering
        if limit is not None or offset:
            sql += " LIMIT " + self.bind(limit if limit is not None else -1)
            if offset:
                sql += " OFFSET " + self.bind(offset)
        dependencies = frozenset().union(*(node.fields for node in self.fragments))
        return Query(sql, self.parameters, fragments, dependencies)
