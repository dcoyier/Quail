"""Preparation and bounded reuse of stored-field search results.

Searches fill indexed TEMP score tables before the compiler uses them. They
always see the whole field corpus, irrespective of the selecting verb's filter.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from quail.contracts import QuailError, fts_table, sql_identifier
from quail.language.expressions import Node
from quail.language.state import State


@dataclass(frozen=True)
class Scores:
    table: str
    field: str
    generation: int


def search_nodes(nodes: Iterable[Node]) -> Iterator[Node]:
    for node in nodes:
        if node.op in {"lexical", "semantic"}:
            yield node
        yield from search_nodes(node.args)


class Searches:
    def __init__(self, state: State) -> None:
        self.state = state
        self.connection = state.connection
        self.cache: OrderedDict[Node, Scores] = OrderedDict()
        self._before_cell: OrderedDict[Node, Scores] = OrderedDict()
        self._serial = 0
        self._queries: OrderedDict[str, str] = OrderedDict()
        # Token extraction does not stem. Porter applies exactly once, in MATCH.
        self.connection.execute(
            "CREATE VIRTUAL TABLE temp.query_words "
            "USING fts5(body, tokenize='unicode61 remove_diacritics 1')"
        )
        self.connection.execute(
            "CREATE VIRTUAL TABLE temp.query_vocab USING fts5vocab('temp','query_words','instance')"
        )

    def begin(self) -> None:
        self._before_cell = self.cache.copy()

    def commit(self) -> None:
        self._before_cell.clear()

    def rollback(self) -> None:
        # SQL rolled back table creation/deletion as well as writes. Restore the
        # matching catalog, then drop affected tag scores rather than salvaging them.
        self.cache = self._before_cell
        self._before_cell = OrderedDict()
        for field in self.state.touched:
            self.invalidate(field)

    def invalidate(self, field: str) -> None:
        for node, item in tuple(self.cache.items()):
            if item.field == field:
                self.connection.execute("DROP TABLE IF EXISTS temp." + sql_identifier(item.table))
                del self.cache[node]

    @contextmanager
    def prepare(self, nodes: Iterable[Node]) -> Generator[dict[Node, str], None, None]:
        selected: dict[Node, str] = {}
        try:
            for node in dict.fromkeys(search_nodes(nodes)):
                field = str(node.args[0].data[0])
                self.state.field_kind(field)
                generation = self.state.generations.get(field, 0)
                item = self.cache.get(node)
                if item is not None and item.generation != generation:
                    self.invalidate(field)
                    item = None
                if item is None:
                    self._serial += 1
                    table = f"scores_{self._serial}"
                    if node.op == "semantic":
                        raise QuailError("Semantic search requires an embedding provider")
                    self._lexical(field, str(node.data[0]), table)
                    item = Scores(table, field, generation)
                    self.cache[node] = item
                self.cache.move_to_end(node)
                selected[node] = item.table
            yield selected
        finally:
            # All results remain pinned until the caller's cursor/statement ends.
            while len(self.cache) > 16:
                _, item = self.cache.popitem(last=False)
                self.connection.execute("DROP TABLE IF EXISTS temp." + sql_identifier(item.table))

    def _tokens(self, text: str) -> list[str]:
        self.connection.execute("DELETE FROM temp.query_words")
        self.connection.execute("INSERT INTO temp.query_words(rowid,body) VALUES (1,?)", (text,))
        return [
            row[0]
            for row in self.connection.execute("SELECT term FROM temp.query_vocab ORDER BY offset")
        ]

    def query(self, text: str) -> str:
        if text in self._queries:
            self._queries.move_to_end(text)
            return self._queries[text]
        spans = text.split('"')
        if len(spans) % 2 == 0:
            raise QuailError("Lexical query has an unclosed double quote")
        atoms: list[str] = []
        for i, span in enumerate(spans):
            tokens = self._tokens(span)
            if i % 2 and tokens:
                atoms.append('"' + " ".join(tokens).replace('"', '""') + '"')
            elif not i % 2:
                atoms.extend('"' + token.replace('"', '""') + '"' for token in tokens)
        if not atoms:
            raise QuailError("Lexical query contains no words")
        result = " OR ".join(atoms)
        self._queries[text] = result
        if len(self._queries) > 128:
            self._queries.popitem(last=False)
        return result

    def _lexical(self, field: str, query: str, destination: str) -> None:
        match = self.query(query)
        source = field in self.state.source.fields
        name = fts_table(field)
        table = ("main." if source else "temp.") + sql_identifier(name)
        if not source and field not in self.state.fts:
            self.connection.execute(
                f"CREATE VIRTUAL TABLE {table} "
                "USING fts5(body, tokenize='porter unicode61 remove_diacritics 1')"
            )
            self.connection.execute(
                f"INSERT INTO {table}(rowid,body) "
                "SELECT e.rowid,q_value('text',t.value,'json','text') FROM temp.working_tags AS t "
                "JOIN main.entries AS e ON e.id=t.entry WHERE t.field=?",
                (field,),
            )
            self.state.fts[field] = name
        scores = "temp." + sql_identifier(destination)
        self.connection.execute(
            f"CREATE TABLE {scores} (rowid INTEGER PRIMARY KEY, score REAL NOT NULL)"
        )
        self.connection.execute(f"INSERT INTO {scores} SELECT rowid,0.0 FROM {table}")
        # bm25 must be evaluated in the MATCH query, not from an outer correlated expression.
        cursor = self.connection.execute(
            f"SELECT rowid,-bm25({sql_identifier(name)}) FROM {table} "
            f"WHERE {sql_identifier(name)} MATCH ?",
            (match,),
        )
        try:
            while rows := cursor.fetchmany(512):
                self.connection.executemany(
                    f"UPDATE {scores} SET score=? WHERE rowid=?",
                    ((score, rowid) for rowid, score in rows),
                )
        finally:
            cursor.close()
