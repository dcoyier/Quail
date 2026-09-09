"""The four verbs and live Entry handles over one private State.

Every projection uses Compiler; every annotation uses State.apply. The evaluator
coordinates those operations and their cache lifetime, without process, project,
provider, or durable-history dependencies.
"""

from __future__ import annotations

import itertools
import reprlib
import sqlite3
from collections import Counter, OrderedDict
from collections.abc import Generator, Hashable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import cast

from quail.contracts import (
    JSONObject,
    JSONValue,
    QuailError,
    TagDelta,
    canonical_json,
    decode_json,
    json_value,
)
from quail.language.compiler import Compiler, Query
from quail.language.expressions import Expression, Field, Node, Predicate, literal
from quail.language.scalars import SQLValue, decode
from quail.language.search import Searches
from quail.language.state import State


@dataclass(frozen=True)
class Row:
    rowid: int
    entry: str
    source: tuple[str | None, ...]
    tags: dict[str, JSONValue]
    epoch: int
    size: int


class Evaluator:
    def __init__(self, state: State) -> None:
        self.state = state
        self.searches = Searches(state)
        self._entries: OrderedDict[str, Row] = OrderedDict()
        self._entry_bytes = 0
        # Combined with SQLite's 1/16 and search's matrix allocation, this leaves
        # most of the RSS budget for ordinary Python and temporary query results.
        self._entry_budget = max(0, state.limits.memory_mb * 1024 * 1024 // 16)

    def begin(self) -> None:
        self.state.begin()
        self.searches.begin()

    def commit(self) -> TagDelta:
        # Materializing/validating the delta is still inside the cell's limits and
        # transaction. Failure here must be able to roll back the private writes.
        delta = self.state.delta()
        self.state.commit()
        self.searches.commit()
        return delta

    def rollback(self) -> None:
        self.state.rollback()
        self.searches.rollback()

    def fields(self) -> list[JSONObject]:
        return self.state.fields()

    def _expression(self, value: object) -> Node:
        if not isinstance(value, Expression):
            raise QuailError("Expected an expression", 'Use Field("name") to refer to a column')
        if value._scope is not self.state:
            raise QuailError("Expression belongs to another kernel")
        for field in value._node.fields:
            self.state.field_kind(field)
        return value._node

    def _where(self, value: object) -> Node | None:
        if value is None:
            return None
        if not isinstance(value, Predicate):
            raise QuailError(
                "where must be a Predicate or None",
                "Use symbolic comparisons such as Field('f') == None, not is None",
            )
        return self._expression(value)

    def _rank(self, value: object) -> Node | None:
        if value is None:
            return None
        node = self._expression(value)
        if node.kind != "number":
            raise QuailError("rank must be a number expression", "Use .number() for numeric text")
        return node

    @staticmethod
    def _limit(value: object, label: str, *, allow_none: bool = False) -> int | None:
        if value is None and allow_none:
            return None
        if type(value) is not int or value < 0:
            raise QuailError(f"{label} must be a nonnegative integer, excluding bool")
        return value

    @contextmanager
    def _query(
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
    ) -> Generator[tuple[sqlite3.Cursor, Query], None, None]:
        nodes = [*projections, *(item for item in (where, rank) if item is not None)]
        with self.searches.prepare(nodes) as prepared:
            query = Compiler(self.state.source.fields, prepared).select(
                projections,
                where=where,
                rank=rank,
                limit=limit,
                offset=offset,
                aggregate=aggregate,
                identity=identity,
                restrict_ids=restrict_ids,
                as_json=as_json,
            )
            cursor = self.state.connection.execute(query.sql, query.parameters)
            try:
                yield cursor, query
            finally:
                cursor.close()

    def count(self, where: object = None, by: object = None) -> int | Counter[Hashable]:
        selected = self._where(where)
        if by is None:
            with self._query([], where=selected, aggregate=True) as (cursor, _):
                return int(cursor.fetchone()[0])
        cross = isinstance(by, list)
        groupings = by if isinstance(by, list) else [by]
        nodes = [self._expression(value) for value in groupings]
        counter: Counter[Hashable] = Counter()
        with self._query(nodes, where=selected, identity=True) as (cursor, query):
            for row in cursor:
                values = [
                    decode(value, fragment.encoding)
                    for value, fragment in zip(row[2:], query.projections, strict=True)
                ]
                for items in itertools.product(*(_contributions(value) for value in values)):
                    counter[items if cross else items[0]] += 1
        return Counter(dict(counter.most_common()))

    def values(
        self, expr: object, where: object = None, rank: object = None, limit: object = None
    ) -> list[JSONValue]:
        node = self._expression(expr)
        with self._query(
            [node],
            where=self._where(where),
            rank=self._rank(rank),
            limit=self._limit(limit, "limit", allow_none=True),
        ) as (cursor, query):
            return [decode(row[0], query.projections[0].encoding) for row in cursor]

    def retrieve(
        self, where: object = None, rank: object = None, limit: object = 10, offset: object = 0
    ) -> list[Entry]:
        count = self._limit(limit, "limit")
        skip = self._limit(offset, "offset")
        assert count is not None and skip is not None
        if count > self.state.limits.max_limit:
            print(f"[retrieve limit clamped from {count} to {self.state.limits.max_limit}]")
            count = self.state.limits.max_limit
        ranking = self._rank(rank)
        columns = [Field(self.state, name)._node for name in self.state.source.fields[1:]]
        with self._query(
            [*columns, ranking if ranking is not None else literal(None)],
            where=self._where(where),
            rank=ranking,
            limit=count,
            offset=skip,
            identity=True,
        ) as (cursor, query):
            # Source values are immutable. Fetch only selected rows and load their
            # tags in bounded batches, before printing can request individual cells.
            rows = cast(list[tuple[SQLValue, ...]], cursor.fetchall())
        self._populate(rows)
        result = []
        for row in rows:
            assert isinstance(row[0], int) and isinstance(row[1], str)
            score = decode(row[-1], query.projections[-1].encoding)
            if score is not None and not isinstance(score, (int, float)):
                raise TypeError("Ranking query returned a nonnumeric score")
            result.append(Entry(self, row[0], row[1], score))
        return result

    def tag(self, target: object, field: str, value: object) -> int:
        if not isinstance(field, str) or not field or "\0" in field:
            raise QuailError("Tag field must be non-empty text without NUL")
        json_value(field)
        if field in self.state.source.fields:
            raise QuailError(f"Cannot tag source field {field!r}")
        node = self._expression(value) if isinstance(value, Expression) else literal(value)
        entries: list[Entry] | None = None
        selected = None
        if isinstance(target, Entry):
            entries = [target]
        elif isinstance(target, list):
            if any(not isinstance(item, Entry) for item in target):
                raise QuailError("Tag entry lists may contain only Entries")
            entries = [item for item in target if isinstance(item, Entry)]
        else:
            selected = self._where(target)
        if entries is not None:
            for entry in entries:
                if entry._evaluator is not self:
                    raise QuailError("Tag entry belongs to another kernel or source snapshot")
        connection = self.state.connection
        connection.execute("DELETE FROM temp.tag_targets")
        if entries is not None:
            connection.executemany(
                "INSERT OR IGNORE INTO temp.tag_targets VALUES (?)",
                ((entry.id,) for entry in entries),
            )
        connection.execute("DELETE FROM temp.tag_stage")
        # Stage directly from the shared compiler's SELECT. No SELECT per target,
        # and no Python copy of the full target/value set before writing it.
        with self.searches.prepare(
            [node, *([selected] if selected is not None else [])]
        ) as prepared:
            query = Compiler(self.state.source.fields, prepared).select(
                [node],
                where=selected,
                identity=True,
                restrict_ids=entries is not None,
                as_json=True,
            )
            connection.execute("INSERT INTO temp.tag_stage " + query.sql, query.parameters)
        result = self.state.apply(field)
        if result:
            self.searches.invalidate(field)
        return result

    def _populate(self, rows: list[tuple[SQLValue, ...]]) -> None:
        for start in range(0, len(rows), 256):
            batch = rows[start : start + 256]
            ids = [row[1] for row in batch]
            tags: dict[str, dict[str, JSONValue]] = {}
            sizes: dict[str, int] = {}
            placeholders = ",".join("?" for _ in ids)
            cursor = self.state.connection.execute(
                f"SELECT entry,field,value FROM temp.working_tags WHERE entry IN ({placeholders})",
                ids,
            )
            try:
                for entry, field, value in cursor:
                    tags.setdefault(entry, {})[field] = decode_json(value)
                    sizes[entry] = sizes.get(entry, 0) + len(field.encode()) + len(value.encode())
            finally:
                cursor.close()
            for row in batch:
                rowid, entry = row[:2]
                assert isinstance(rowid, int) and isinstance(entry, str)
                source = cast(tuple[str | None, ...], row[1:-1])
                size = (
                    sizes.get(entry, 0)
                    + sum(len(value.encode()) if value is not None else 0 for value in source)
                    + 128 * (len(source) + len(tags.get(entry, {})))
                )
                self._remember(
                    Row(rowid, entry, source, tags.get(entry, {}), self.state.epoch, size)
                )

    def _remember(self, row: Row) -> None:
        old = self._entries.pop(row.entry, None)
        if old is not None:
            self._entry_bytes -= old.size
        if row.size <= self._entry_budget:
            self._entries[row.entry] = row
            self._entry_bytes += row.size
        while self._entry_bytes > self._entry_budget:
            _, removed = self._entries.popitem(last=False)
            self._entry_bytes -= removed.size

    def _row(self, entry: str) -> Row:
        current = self._entries.get(entry)
        if current is not None and current.epoch == self.state.epoch:
            self._entries.move_to_end(entry)
            return current
        # One bulk refresh for an Entry, not one lookup for each field it displays.
        if current is None:
            source = self.state.connection.execute(
                "SELECT rowid,* FROM main.entries WHERE id=?", (entry,)
            ).fetchone()
            if source is None:
                raise QuailError("Entry is outside this source snapshot")
            rowid, source_values = source[0], tuple(source[1:])
        else:
            rowid, source_values = current.rowid, current.source
        tags = {
            field: decode_json(value)
            for field, value in self.state.connection.execute(
                "SELECT field,value FROM temp.working_tags WHERE entry=?", (entry,)
            )
        }
        size = sum(len(value.encode()) if value is not None else 0 for value in source_values)
        size += len(canonical_json(tags).encode()) + 128 * (len(source_values) + len(tags))
        row = Row(rowid, entry, source_values, tags, self.state.epoch, size)
        self._remember(row)
        return row

    def entry_value(self, entry: Entry, expr: Expression) -> JSONValue:
        node = self._expression(expr)
        selected = (Field(self.state, "id") == entry.id)._node
        with self._query([node], where=selected) as (cursor, query):
            row = cursor.fetchone()
            return decode(row[0], query.projections[0].encoding)

    def close(self) -> None:
        self._entries.clear()
        self.state.close()


def _contributions(value: JSONValue) -> list[Hashable]:
    items = value if isinstance(value, list) else [value]
    return [
        ("json", canonical_json(item)) if isinstance(item, (list, dict)) else item for item in items
    ]


class Entry(Mapping[str, JSONValue]):
    """A read-only live handle. Materialize dict(entry) for a value snapshot."""

    __slots__ = ("_evaluator", "_rowid", "_id", "_score")

    def __init__(
        self, evaluator: Evaluator, rowid: int, entry: str, score: int | float | None
    ) -> None:
        self._evaluator = evaluator
        self._rowid = rowid
        self._id = entry
        self._score = score

    @property
    def id(self) -> str:
        return self._id

    @property
    def score(self) -> int | float | None:
        return self._score

    def __iter__(self) -> Iterator[str]:
        state = self._evaluator.state
        return iter((*state.source.fields, *sorted(state.counts)))

    def __len__(self) -> int:
        state = self._evaluator.state
        return len(state.source.fields) + len(state.counts)

    def __getitem__(self, key: str | Expression) -> JSONValue:
        if isinstance(key, Expression):
            return self._evaluator.entry_value(self, key)
        state = self._evaluator.state
        if key not in state.source.fields and key not in state.counts:
            raise KeyError(key)
        row = self._evaluator._row(self.id)
        if key in state.source.fields:
            return row.source[state.source.fields.index(key)]
        # Reading a list/dict never grants a reference into the annotation cache.
        return json_value(row.tags.get(key))

    def __repr__(self) -> str:
        row = self._evaluator._row(self.id)
        values = dict(zip(self._evaluator.state.source.fields, row.source, strict=True)) | row.tags
        rendered = ", ".join(f"{name!r}: {_preview(values.get(name))}" for name in self)
        return f"Entry({{{rendered}}}, score={self.score!r})"


def _preview(value: JSONValue) -> str:
    if isinstance(value, str):
        if len(value) > 240:
            return repr(value[:240]) + f"… [{len(value)} characters; full value via entry lookup]"
        return repr(value)
    return reprlib.repr(value)
