"""One connection and one mutation path for a kernel's private working tags.

Tables and statements are reused across ordinary Python annotation loops. All
writes in a cell share its TEMP transaction; only the final write set crosses
to the host. The catalog is small metadata, restored alongside SQL on failure.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from quail.contracts import (
    FieldInfo,
    Limits,
    QuailError,
    Source,
    TagDelta,
    decode_json,
    sql_identifier,
)
from quail.language.expressions import Kind
from quail.language.scalars import register


class State:
    def __init__(self, path: Path, source: Source, session: str, limits: Limits) -> None:
        self.source = source
        self.limits = limits
        self.connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=5
        )
        self.counts: dict[str, int] = {}
        self.generations: dict[str, int] = {}
        self.epoch = 0
        self.fts: dict[str, str] = {}
        self.touched: set[str] = set()
        self._saved_counts: dict[str, int] | None = None
        self._saved_fts: dict[str, str] = {}
        self._callback_error: BaseException | None = None
        try:
            self._bootstrap(session)
        except BaseException:
            self.connection.close()
            raise

    def _bootstrap(self, session: str) -> None:
        connection = self.connection
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA temp_store=FILE")
        options = {row[0] for row in connection.execute("PRAGMA compile_options")}
        if "TEMP_STORE=3" in options:
            raise QuailError("This SQLite build cannot provide file-backed TEMP storage")
        page_kib = max(64, min(8192, self.limits.memory_mb * 1024 // 32))
        connection.execute(f"PRAGMA cache_size=-{page_kib}")
        connection.execute(f"PRAGMA temp.cache_size=-{page_kib}")
        if hasattr(connection, "enable_load_extension"):
            connection.enable_load_extension(False)
        register(connection, self._failed_callback)
        for statement in (
            "CREATE TEMP TABLE working_tags (entry TEXT, field TEXT, value TEXT NOT NULL, "
            "PRIMARY KEY(entry, field)) WITHOUT ROWID",
            "CREATE INDEX temp.working_field ON working_tags(field)",
            "CREATE TEMP TABLE tag_stage (rowid INTEGER, entry TEXT PRIMARY KEY, value TEXT)",
            "CREATE TEMP TABLE tag_targets (entry TEXT PRIMARY KEY) WITHOUT ROWID",
            "CREATE TEMP TABLE cell_writes (field TEXT, entry TEXT, value TEXT, "
            "PRIMARY KEY(field, entry)) WITHOUT ROWID",
        ):
            connection.execute(statement)
        connection.execute("BEGIN")
        try:
            connection.execute(
                "INSERT INTO temp.working_tags SELECT entry,field,value "
                "FROM main.tags WHERE session=?",
                (session,),
            )
            self.counts = dict(
                connection.execute("SELECT field,count(*) FROM temp.working_tags GROUP BY field")
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def field_kind(self, name: str) -> Kind:
        if name in self.source.fields:
            return "text"
        if name in self.counts:
            return "any"
        choices = ", ".join((*self.source.fields, *sorted(self.counts)))
        raise QuailError(f"Unknown field: {name!r}", f"Available fields: {choices}")

    def _failed_callback(self, error: BaseException) -> None:
        self._callback_error = error

    @contextmanager
    def evaluation(self) -> Generator[None, None, None]:
        """Preserve a scalar callback's exception across SQLite's C boundary."""
        self._callback_error = None
        try:
            yield
        except sqlite3.Error:
            if self._callback_error is not None:
                raise self._callback_error from None
            raise
        finally:
            self._callback_error = None

    def fields(self) -> list[FieldInfo]:
        result = [
            FieldInfo(name, "source", count)
            for name, count in zip(self.source.fields, self.source.present, strict=True)
        ]
        result.extend(FieldInfo(name, "tag", self.counts[name]) for name in sorted(self.counts))
        return result

    def begin(self) -> None:
        if self._saved_counts is not None:
            raise RuntimeError("A cell is already running")
        self.connection.execute("DELETE FROM temp.cell_writes")
        self.connection.execute("BEGIN")
        self._saved_counts = self.counts.copy()
        self._saved_fts = self.fts.copy()
        self.touched.clear()

    def _advance(self, field: str) -> None:
        self.epoch += 1
        self.generations[field] = self.epoch

    def apply(self, field: str) -> int:
        """Apply the fully resolved tag_stage; it cannot change while being scanned."""
        if self._saved_counts is None:
            raise RuntimeError("Tags must be written inside a cell")
        connection = self.connection
        count, difference = connection.execute(
            "SELECT count(*), coalesce(sum(s.value IS NOT NULL)-sum(t.value IS NOT NULL),0) "
            "FROM temp.tag_stage AS s LEFT JOIN temp.working_tags AS t "
            "ON t.entry=s.entry AND t.field=?",
            (field,),
        ).fetchone()
        if count == 0:
            return 0
        connection.execute(
            "DELETE FROM temp.working_tags WHERE field=? "
            "AND entry IN (SELECT entry FROM temp.tag_stage WHERE value IS NULL)",
            (field,),
        )
        connection.execute(
            "INSERT INTO temp.working_tags SELECT entry,?,value "
            "FROM temp.tag_stage WHERE value IS NOT NULL "
            "ON CONFLICT(entry,field) DO UPDATE SET value=excluded.value",
            (field,),
        )
        if field in self.fts:
            table = "temp." + sql_identifier(self.fts[field])
            connection.execute(
                f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM temp.tag_stage)"
            )
            connection.execute(
                f"INSERT INTO {table}(rowid,body) SELECT rowid,q_value('text',value,'json','text') "
                "FROM temp.tag_stage WHERE value IS NOT NULL"
            )
        connection.execute(
            "INSERT INTO temp.cell_writes SELECT ?,entry,value FROM temp.tag_stage WHERE true "
            "ON CONFLICT(field,entry) DO UPDATE SET value=excluded.value",
            (field,),
        )
        present = self.counts.get(field, 0) + difference
        if present:
            self.counts[field] = present
        else:
            self.counts.pop(field, None)
        self.touched.add(field)
        self._advance(field)
        return int(count)

    def delta(self) -> TagDelta:
        result: TagDelta = {}
        for field, entry, value in self.connection.execute(
            "SELECT field,entry,value FROM temp.cell_writes"
        ):
            result.setdefault(field, {})[entry] = decode_json(value) if value is not None else None
        return result

    def commit(self) -> None:
        self.connection.execute("COMMIT")
        self._saved_counts = None
        self._saved_fts.clear()

    def rollback(self) -> None:
        if self._saved_counts is None:
            raise RuntimeError("No cell to roll back")
        if self.connection.in_transaction:
            self.connection.execute("ROLLBACK")
        self.counts = self._saved_counts
        self.fts = self._saved_fts
        self._saved_counts = None
        self._saved_fts = {}
        # Revisions never go backwards, even when values do.
        for field in self.touched:
            self._advance(field)

    def close(self) -> None:
        self.connection.close()
