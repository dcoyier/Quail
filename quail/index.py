"""Derived dataset storage: source import and committed session materialization.

An Index owns one connection on its calling thread. Its public write operations
own short, explicit transactions. History is staged privately and validated in
full before replacing a committed view; CSV replacements are separate files.
"""

from __future__ import annotations

import csv
import hashlib
import io
import itertools
import math
import os
import sqlite3
import struct
import sys
from collections.abc import Buffer, Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from quail import history
from quail.contracts import (
    JSONObject,
    QuailError,
    Source,
    TagDelta,
    canonical_json,
    checked_hash,
    decode_json,
    fts_table,
    json_object,
    source_version,
    sql_identifier,
)
from quail.project import DatasetConfig, SessionMetadata, sync_directory

SCHEMA_VERSION = 1
FTS_TOKENIZER = "porter unicode61 remove_diacritics 1"


@contextmanager
def transaction(
    connection: sqlite3.Connection, *, immediate: bool = False
) -> Generator[None, None, None]:
    """The owning operation commits; nested transactions are programming errors."""
    if connection.in_transaction:
        raise RuntimeError("An index operation cannot own a nested transaction")
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rwc")
    connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA cache_size=-8192")
        connection.execute("PRAGMA temp.cache_size=-8192")
        # Some macOS Python builds omit extension loading entirely.
        if hasattr(connection, "enable_load_extension"):
            connection.enable_load_extension(False)
        if not readonly:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection
    except BaseException:
        connection.close()
        raise


@dataclass(frozen=True)
class Applied:
    digest: str
    source_version: str
    orphans: int
    summary: history.Summary


@dataclass(frozen=True)
class StoredVectors:
    vectors: tuple[bytes, ...]
    inserted: int


def validate_vector(packed: bytes, dimensions: int | None = None) -> int:
    """Validate the canonical float32 representation, with a safe nonzero norm."""
    if not packed or len(packed) % 4:
        raise QuailError("An embedding must contain complete float32 coordinates")
    size = len(packed) // 4
    if dimensions is not None and dimensions != size:
        raise QuailError(f"Embedding dimensions changed: expected {dimensions}, got {size}")
    values = [value for (value,) in struct.iter_unpack("<f", packed)]
    if any(not math.isfinite(value) for value in values):
        raise QuailError("An embedding contains non-finite float32 coordinates")
    # Float32 sum-of-squares can overflow or underflow for valid vectors. hypot
    # accumulates safely on the packed values, including float32 subnormals.
    if math.hypot(*values) == 0:
        raise QuailError("An embedding must have a nonzero norm after float32 packing")
    return size


def pack_vector(values: Sequence[float]) -> bytes:
    if not values or any(type(value) not in {int, float} for value in values):
        raise QuailError("An embedding must be a non-empty array of numbers")
    try:
        packed = struct.pack(f"<{len(values)}f", *values)
    except (OverflowError, struct.error) as error:
        raise QuailError("Embedding coordinates do not fit finite float32 values") from error
    validate_vector(packed)
    return packed


class Index:
    def __init__(self, path: Path, connection: sqlite3.Connection, source: Source) -> None:
        self.path = path
        self.connection = connection
        self.source = source

    @classmethod
    def open(cls, path: Path, *, readonly: bool = False) -> Index:
        connection = connect(path, readonly=readonly)
        try:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            if metadata.get("schema") != str(SCHEMA_VERSION):
                raise QuailError("Index cache schema needs rebuilding")
            source = Source.from_record(json_object(decode_json(metadata["source"])))
            return cls(path, connection, source)
        except BaseException:
            connection.close()
            raise

    @classmethod
    def build(cls, path: Path, dataset: DatasetConfig) -> Index:
        """Build at a fresh temporary path; publication belongs to the caller."""
        connection = connect(path)
        try:
            with transaction(connection):
                source = _import_csv(connection, dataset)
                _create_schema(connection)
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    [
                        ("schema", str(SCHEMA_VERSION)),
                        ("source", canonical_json(source.to_record())),
                    ],
                )
            return cls(path, connection, source)
        except BaseException:
            connection.close()
            raise

    def compatible(self, session: SessionMetadata) -> None:
        if self.source.id_column is None and (
            session.id_column is not None or self.source.version != session.source_version
        ):
            raise QuailError(
                f"Session {session.name!r} cannot follow changed generated IDs",
                "Restore its original CSV or supply the original canonical IDs explicitly",
            )

    def applied(self, session: str) -> Applied | None:
        row = self.connection.execute(
            "SELECT digest, source_version, orphans, summary FROM applied WHERE session=?",
            (session,),
        ).fetchone()
        if row is None:
            return None
        return Applied(
            row[0], row[1], row[2], history.Summary.from_record(json_object(decode_json(row[3])))
        )

    def synchronize(self, session: SessionMetadata, snapshot: history.Snapshot) -> Applied:
        """Replay into TEMP; an invalid late record never publishes partial tags."""
        self.compatible(session)
        current = self.applied(session.name)
        if (
            current
            and current.digest == snapshot.digest
            and current.source_version == self.source.version
        ):
            return current
        connection = self.connection
        connection.execute("DROP TABLE IF EXISTS temp.replay_tags")
        connection.execute(
            "CREATE TEMP TABLE replay_tags (entry TEXT, field TEXT, value TEXT, "
            "PRIMARY KEY(entry, field)) WITHOUT ROWID"
        )
        summary = history.Summary()
        try:
            # Only TEMP changes during streaming validation, so no shared writer is held.
            with transaction(connection):
                for _, cell in history.records(snapshot, session, summary):
                    for field, entries in cell.tags.items():
                        connection.executemany(
                            "INSERT INTO temp.replay_tags VALUES (?, ?, ?) "
                            "ON CONFLICT(entry, field) DO UPDATE SET value=excluded.value",
                            (
                                (entry, field, canonical_json(value))
                                for entry, value in entries.items()
                            ),
                        )
                connection.execute("DELETE FROM temp.replay_tags WHERE value='null'")
                recovered = {
                    row[0]
                    for row in connection.execute("SELECT DISTINCT field FROM temp.replay_tags")
                }
                conflicts = recovered.intersection(self.source.fields)
                if conflicts:
                    raise QuailError(
                        f"Source/tag field conflict in {session.name!r}: {sorted(conflicts)}"
                    )
                orphans = connection.execute(
                    "SELECT count(*) FROM temp.replay_tags AS t "
                    "WHERE NOT EXISTS (SELECT 1 FROM main.entries AS e WHERE e.id=t.entry)"
                ).fetchone()[0]
            applied = Applied(snapshot.digest, self.source.version, orphans, summary)
            with transaction(connection):
                connection.execute("DELETE FROM tags WHERE session=?", (session.name,))
                connection.execute(
                    "INSERT INTO tags SELECT ?, t.entry, t.field, t.value "
                    "FROM temp.replay_tags AS t "
                    "JOIN entries AS e ON e.id=t.entry",
                    (session.name,),
                )
                self._write_applied(session.name, applied)
            return applied
        finally:
            connection.execute("DROP TABLE IF EXISTS temp.replay_tags")

    def validate_delta(self, tags: TagDelta) -> None:
        """Child replies are untrusted even though the shared codec decoded them."""
        for field, entries in tags.items():
            if field in self.source.fields:
                raise QuailError(f"Cannot tag source field {field!r}")
            for batch in itertools.batched(entries, 256):
                placeholders = ",".join("?" for _ in batch)
                live = {
                    row[0]
                    for row in self.connection.execute(
                        f"SELECT id FROM entries WHERE id IN ({placeholders})", batch
                    )
                }
                missing = set(batch) - live
                if missing:
                    raise QuailError(f"Tag entry is outside the live source: {min(missing)!r}")

    def complete(self, session: str, tags: TagDelta, applied: Applied) -> None:
        """Publish an already-synced log result and its cache marker atomically."""
        with transaction(self.connection):
            for field, entries in tags.items():
                for entry, value in entries.items():
                    if value is None:
                        self.connection.execute(
                            "DELETE FROM tags WHERE session=? AND entry=? AND field=?",
                            (session, entry, field),
                        )
                    else:
                        self.connection.execute(
                            "INSERT INTO tags VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(session, entry, field) "
                            "DO UPDATE SET value=excluded.value",
                            (session, entry, field, canonical_json(value)),
                        )
            self._write_applied(session, applied)

    def _write_applied(self, session: str, applied: Applied) -> None:
        # Deliberately no transaction here: both callers own their atomic publication.
        self.connection.execute(
            "INSERT INTO applied VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session) DO UPDATE SET "
            "digest=excluded.digest, source_version=excluded.source_version, "
            "orphans=excluded.orphans, "
            "max_order=excluded.max_order, summary=excluded.summary",
            (
                session,
                applied.digest,
                applied.source_version,
                applied.orphans,
                applied.summary.max_order,
                canonical_json(applied.summary.to_record()),
            ),
        )

    def fields(self, session: str | None = None) -> list[JSONObject]:
        result: list[JSONObject] = [
            {"name": field, "kind": "source", "present": present}
            for field, present in zip(self.source.fields, self.source.present, strict=True)
        ]
        if session is not None:
            result.extend(
                {"name": field, "kind": "tag", "present": present}
                for field, present in self.connection.execute(
                    "SELECT field, count(*) FROM tags WHERE session=? "
                    "GROUP BY field ORDER BY field",
                    (session,),
                )
            )
        return result

    def copy_vectors(self, previous: Index) -> None:
        """Preserve the current cache format across source-only rebuilds.

        Exact text hashes and embedding identities remain valid independently of
        row IDs. Pack receipts are not copied: their paths describe source versions.
        """
        cursor = previous.connection.execute("SELECT embedding_id, text_hash, vec FROM vectors")
        try:
            while rows := cursor.fetchmany(256):
                with transaction(self.connection):
                    self.connection.executemany(
                        "INSERT OR IGNORE INTO vectors VALUES (?, ?, ?)", rows
                    )
        finally:
            cursor.close()

    def vector_dimensions(self, embedding: str) -> int | None:
        row = self.connection.execute(
            "SELECT length(vec) FROM vectors WHERE embedding_id=? LIMIT 1", (embedding,)
        ).fetchone()
        return int(row[0]) // 4 if row is not None else None

    def vectors(self, embedding: str, hashes: Sequence[str]) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for batch in itertools.batched(hashes, 256):
            placeholders = ",".join("?" for _ in batch)
            result.update(
                self.connection.execute(
                    f"SELECT text_hash,vec FROM vectors WHERE embedding_id=? "
                    f"AND text_hash IN ({placeholders})",
                    (embedding, *batch),
                )
            )
        return result

    def insert_vectors(self, embedding: str, rows: Sequence[tuple[str, bytes]]) -> StoredVectors:
        """Validate before the writer; recheck dimensions while holding it.

        The first concurrent insertion establishes dimensions. Existing keys win,
        and every caller receives the canonical stored bytes in its input order.
        """
        checked_hash(embedding, "embedding identity")
        if not rows:
            return StoredVectors((), 0)
        dimensions = validate_vector(rows[0][1])
        for text_hash, packed in rows:
            checked_hash(text_hash, "text hash")
            validate_vector(packed, dimensions)
        with transaction(self.connection, immediate=True):
            current = self.vector_dimensions(embedding)
            if current is not None and current != dimensions:
                raise QuailError(
                    f"Embedding dimensions changed: expected {current}, got {dimensions}"
                )
            cursor = self.connection.executemany(
                "INSERT OR IGNORE INTO vectors VALUES (?, ?, ?)",
                ((embedding, text_hash, packed) for text_hash, packed in rows),
            )
            inserted = cursor.rowcount
            stored = self.vectors(embedding, [text_hash for text_hash, _ in rows])
        return StoredVectors(tuple(stored[text_hash] for text_hash, _ in rows), inserted)

    def export_rows(self, session: str, tag_fields: list[str]) -> Iterator[list[str | None]]:
        """Merge two ordered cursors; no per-entry queries or extra-wide SQL rows."""
        sources = self.connection.execute("SELECT * FROM entries ORDER BY rowid")
        tags = self.connection.execute(
            "SELECT t.entry, t.field, t.value FROM tags AS t "
            "JOIN entries AS e ON e.id=t.entry WHERE t.session=? ORDER BY e.rowid, t.field",
            (session,),
        )
        try:
            current = next(tags, None)
            for source in sources:
                values: dict[str, str | None] = {}
                while current is not None and current[0] == source[0]:
                    value = decode_json(current[2])
                    values[current[1]] = value if isinstance(value, str) else canonical_json(value)
                    current = next(tags, None)
                yield [*source, *(values.get(field) for field in tag_fields)]
        finally:
            sources.close()
            tags.close()

    def checkpoint(self) -> None:
        busy, _, _ = self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise QuailError("Index checkpoint is busy")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _create_schema(connection: sqlite3.Connection) -> None:
    statements = (
        "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE tags (session TEXT, entry TEXT REFERENCES entries(id), field TEXT, "
        "value TEXT NOT NULL, PRIMARY KEY(session, entry, field)) WITHOUT ROWID",
        "CREATE INDEX tags_field ON tags(session, field)",
        "CREATE TABLE applied (session TEXT PRIMARY KEY, digest TEXT, source_version TEXT, "
        "orphans INTEGER, max_order INTEGER, summary TEXT)",
        "CREATE TABLE vectors (embedding_id TEXT, text_hash TEXT, vec BLOB NOT NULL, "
        "PRIMARY KEY(embedding_id, text_hash)) WITHOUT ROWID",
        "CREATE TABLE ingested (path TEXT PRIMARY KEY, file_hash TEXT NOT NULL)",
    )
    for statement in statements:
        connection.execute(statement)


def hash_source(dataset: DatasetConfig) -> str:
    try:
        with dataset.source.open("rb") as stream:
            return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as error:
        raise QuailError(f"Cannot read source {dataset.source}: {error}") from error


class _HashedReader(io.RawIOBase):
    """Hash the very bytes consumed by CSV decoding, including BOM and newlines."""

    def __init__(self, stream: io.BufferedReader) -> None:
        self.stream = stream
        self.digest = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        count = self.stream.readinto(buffer)
        self.digest.update(memoryview(buffer)[:count])
        return count


def _ascii_fold(value: str) -> str:
    return value.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def _headers(
    header: list[str], selected: str | None, limit: int
) -> tuple[tuple[str, ...], int | None]:
    if not header:
        raise QuailError("CSV header is empty")
    if any(not field or "\0" in field for field in header):
        raise QuailError("CSV header names must be non-empty and contain no NUL")
    resolved = selected if selected is not None else ("id" if "id" in header else None)
    if resolved is not None and resolved not in header:
        raise QuailError(f"Selected ID column does not exist: {resolved!r}")
    if resolved != "id" and resolved is not None and "id" in header:
        raise QuailError("Selected ID column conflicts with the existing id column")
    position = header.index(resolved) if resolved is not None else None
    fields = ("id", *(field for i, field in enumerate(header) if i != position))
    folded = [_ascii_fold(field) for field in fields]
    if len(set(folded)) != len(folded) or "rowid" in folded:
        raise QuailError("CSV header has duplicate public names or the reserved rowid name")
    if len(fields) > limit:
        raise QuailError(f"CSV has {len(fields)} public fields; this SQLite build supports {limit}")
    return fields, position


def _import_csv(connection: sqlite3.Connection, dataset: DatasetConfig) -> Source:
    csv.field_size_limit(sys.maxsize)
    rows = 0
    try:
        with dataset.source.open("rb") as raw:
            before = os.fstat(raw.fileno())
            hashed = _HashedReader(raw)
            with io.TextIOWrapper(
                io.BufferedReader(hashed), encoding="utf-8-sig", newline=""
            ) as text:
                reader = csv.reader(text, strict=True)
                header = next(reader, [])
                fields, position = _headers(
                    header, dataset.id_column, connection.getlimit(sqlite3.SQLITE_LIMIT_COLUMN)
                )
                columns = ", ".join(sql_identifier(field) + " TEXT" for field in fields)
                connection.execute(f"CREATE TABLE entries ({columns}, UNIQUE(id))")
                insert = "INSERT INTO entries VALUES (" + ",".join("?" for _ in fields) + ")"
                present = [0] * len(fields)
                for rows, row in enumerate(reader, 1):
                    if len(row) != len(header):
                        raise QuailError(f"Expected {len(header)} cells, got {len(row)}")
                    entry = row[position] if position is not None else f"row-{rows:06d}"
                    if not entry:
                        raise QuailError("Entry ID cannot be empty")
                    values = [
                        entry,
                        *(value or None for i, value in enumerate(row) if i != position),
                    ]
                    try:
                        connection.execute(insert, values)
                    except sqlite3.IntegrityError as error:
                        raise QuailError(f"Duplicate entry ID: {entry!r}") from error
                    for i, value in enumerate(values):
                        present[i] += value is not None
                after = os.fstat(raw.fileno())
                current = dataset.source.stat()
                if _file_state(before) != _file_state(after) or _file_state(after) != _file_state(
                    current
                ):
                    raise QuailError("Source changed during import; retry when the file is stable")
                digest = "sha256:" + hashed.digest.hexdigest()
        for field in fields[1:]:
            table, column = sql_identifier(fts_table(field)), sql_identifier(field)
            connection.execute(
                f"CREATE VIRTUAL TABLE {table} USING fts5(body, tokenize='{FTS_TOKENIZER}')"
            )
            connection.execute(
                f"INSERT INTO {table}(rowid, body) SELECT rowid, {column} "
                f"FROM entries WHERE {column} IS NOT NULL"
            )
        id_column = header[position] if position is not None else None
        return Source(
            digest, source_version(digest, id_column), id_column, fields, tuple(present), rows
        )
    except (OSError, UnicodeError, csv.Error, QuailError) as error:
        location = f"source record {rows + 1}" if rows else "header"
        raise QuailError(f"{dataset.source.name}, {location}: {error}") from error


def _file_state(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def publish(replacement: Index, destination: Path) -> None:
    """Publish only a checkpointed, closed replacement under an exclusive dataset lock."""
    replacement.checkpoint()
    replacement.close()
    # Old sidecars are disposable only after all old owners have released their locks.
    for suffix in ("-wal", "-shm"):
        Path(str(destination) + suffix).unlink(missing_ok=True)
    os.replace(replacement.path, destination)
    sync_directory(destination.parent)
