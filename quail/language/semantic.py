"""Exact cosine search over a field's distinct complete values.

Mappings and packed vectors live in file-backed TEMP tables. Normalized matrices
are disposable accelerators with one shared memory allowance; larger fields use
the same numerical operations in bounded batches. This module knows only the
embedding identity and a packed-vector callback, never provider configuration.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from quail.contracts import (
    QuailError,
    canonical_json,
    decode_json,
    digest_bytes,
    sql_identifier,
    text_value,
)
from quail.language.state import State

type Embed = Callable[[list[str]], Sequence[bytes]]
type Matrix = NDArray[np.float32]
type RowIDs = NDArray[np.int64]

_TEXT_ITEMS = 128
_TEXT_BYTES = 256 * 1024


def normalize(packed: Sequence[bytes], dimensions: int) -> Matrix:
    """Normalize packed float32 with float64 norms, including subnormal values.

    Resident and streamed scoring share this path. Tests compare both to scalar
    cosine with absolute tolerance 1e-5; bitwise BLAS agreement is not a contract.
    """
    if dimensions <= 0 or any(len(vector) != dimensions * 4 for vector in packed):
        raise QuailError("Semantic vectors have inconsistent dimensions")
    matrix = np.frombuffer(b"".join(packed), dtype="<f4").reshape(-1, dimensions).copy()
    norms = np.sqrt(np.einsum("ij,ij->i", matrix, matrix, dtype=np.float64))
    if not np.isfinite(matrix).all() or not np.isfinite(norms).all() or np.any(norms == 0):
        raise QuailError("Semantic vectors must be finite and have nonzero norms")
    matrix /= norms[:, None]
    return matrix


def cosine(matrix: Matrix, query: Matrix) -> Matrix:
    # A dot product of normalized vectors may overshoot an endpoint by rounding.
    return cast(Matrix, np.clip(matrix @ query[0], -1.0, 1.0))


@dataclass(frozen=True)
class FieldVectors:
    values: str
    mapping: str
    generation: int


@dataclass(frozen=True)
class Resident:
    rows: RowIDs
    matrix: Matrix

    @property
    def size(self) -> int:
        return self.rows.nbytes + self.matrix.nbytes


class Semantic:
    def __init__(self, state: State, identity: str | None, embed: Embed | None) -> None:
        self.state = state
        self.connection = state.connection
        self.identity, self.embed = identity, embed
        self.fields: OrderedDict[str, FieldVectors] = OrderedDict()
        self._before_cell: OrderedDict[str, FieldVectors] = OrderedDict()
        self.matrices: OrderedDict[str, Resident] = OrderedDict()
        self._serial = 0
        # Entries use 1/16, SQLite at most 1/16, and the runner at most 1/32.
        # Retained matrices take 1/8; batch allocation has a separate 1/32 ceiling.
        allowance = state.limits.memory_mb * 1024 * 1024
        self.matrix_budget = allowance // 8
        self.batch_budget = max(4096, allowance // 32)

    def begin(self) -> None:
        self._before_cell = self.fields.copy()

    def commit(self) -> None:
        self._before_cell.clear()

    def rollback(self) -> None:
        self.fields = self._before_cell
        self._before_cell = OrderedDict()
        restored = {item.values for item in self.fields.values()}
        # Do not snapshot arrays at begin: that would keep evicted allocations
        # alive for an entire cell and defeat the aggregate memory budget.
        for key in tuple(self.matrices):
            if key not in restored:
                del self.matrices[key]

    def invalidate(self, field: str) -> None:
        item = self.fields.pop(field, None)
        if item is not None:
            self.matrices.pop(item.values, None)
            self.connection.execute(f"DROP TABLE IF EXISTS temp.{item.mapping}")
            self.connection.execute(f"DROP TABLE IF EXISTS temp.{item.values}")

    def _prepare(self, field: str) -> FieldVectors:
        generation = self.state.generations.get(field, 0)
        item = self.fields.get(field)
        if item is not None and item.generation != generation:
            self.invalidate(field)
            item = None
        if item is None:
            self._serial += 1
            item = FieldVectors(
                f"semantic_values_{self._serial}", f"semantic_mapping_{self._serial}", generation
            )
            self.fields[field] = item
            try:
                self._inventory(field, item)
            except BaseException:
                self.invalidate(field)
                raise
        self.fields.move_to_end(field)
        self._fill(item)
        # Catalog eviction is independent of score-table lifetime: completed
        # score tables contain their own entry scores, not joins to this mapping.
        while len(self.fields) > 8:
            self.invalidate(next(iter(self.fields)))
        return item

    def _inventory(self, field: str, item: FieldVectors) -> None:
        connection = self.connection
        connection.execute(
            f"CREATE TABLE temp.{item.values} "
            "(rowid INTEGER PRIMARY KEY, text_hash TEXT UNIQUE, body TEXT NOT NULL, vec BLOB)"
        )
        connection.execute(
            f"CREATE INDEX temp.{item.values}_missing ON {item.values}(rowid) WHERE vec IS NULL"
        )
        connection.execute(
            f"CREATE TABLE temp.{item.mapping} (rowid INTEGER PRIMARY KEY, text_hash TEXT NOT NULL)"
        )
        # Use the same scalar conversion as the verbs; an empty rendering has no
        # semantic document, even when the underlying tag is present.
        source = field in self.state.source.fields
        if source:
            cursor = connection.execute(f"SELECT rowid,{sql_identifier(field)} FROM main.entries")
        else:
            cursor = connection.execute(
                "SELECT e.rowid,t.value FROM temp.working_tags AS t "
                "JOIN main.entries AS e ON e.id=t.entry WHERE t.field=?",
                (field,),
            )
        try:
            while rows := cursor.fetchmany(256):
                values, mappings = [], []
                for rowid, value in rows:
                    text = value if source else text_value(decode_json(value))
                    if text:
                        text_hash = digest_bytes(text.encode("utf-8"))
                        values.append((text_hash, text))
                        mappings.append((rowid, text_hash))
                connection.executemany(
                    f"INSERT OR IGNORE INTO temp.{item.values}(text_hash,body) VALUES (?,?)", values
                )
                connection.executemany(f"INSERT INTO temp.{item.mapping} VALUES (?,?)", mappings)
        finally:
            cursor.close()
        connection.execute(
            f"UPDATE temp.{item.values} SET vec=(SELECT v.vec FROM main.vectors AS v "
            f"WHERE v.embedding_id=? AND v.text_hash={item.values}.text_hash)",
            (self.identity,),
        )

    def _request(self, texts: list[str]) -> Sequence[bytes]:
        if self.embed is None:
            raise QuailError("Semantic search requires an embedding provider")
        vectors = self.embed(texts)
        if len(vectors) != len(texts):
            raise QuailError("Embedding reply count does not match the requested texts")
        return vectors

    def _fill(self, item: FieldVectors) -> None:
        while True:
            cursor = self.connection.execute(
                f"SELECT rowid,body FROM temp.{item.values} WHERE vec IS NULL LIMIT ?",
                (_TEXT_ITEMS,),
            )
            try:
                batch: list[tuple[int, str]] = []
                size = 0
                for rowid, text in cursor:
                    encoded = len(canonical_json(text).encode("utf-8"))
                    if batch and size + encoded > _TEXT_BYTES:
                        break
                    batch.append((rowid, text))
                    size += encoded
            finally:
                cursor.close()
            if not batch:
                return
            vectors = self._request([text for _, text in batch])
            # A host insertion may be newer than our read snapshot. Retain the
            # returned canonical bytes directly instead of re-reading main.
            self.connection.executemany(
                f"UPDATE temp.{item.values} SET vec=? WHERE rowid=?",
                ((vector, rowid) for (rowid, _), vector in zip(batch, vectors, strict=True)),
            )

    def _batches(self, item: FieldVectors, dimensions: int) -> Iterator[Resident]:
        # Allow packed rows, the joined buffer, normalized data, and SQL/Python
        # overhead concurrently. An individual vector is the indivisible unit.
        count = max(1, min(512, self.batch_budget // (dimensions * 4 * 4 + 256)))
        cursor = self.connection.execute(f"SELECT rowid,vec FROM temp.{item.values} ORDER BY rowid")
        try:
            while rows := cursor.fetchmany(count):
                yield Resident(
                    np.array([rowid for rowid, _ in rows], dtype=np.int64),
                    normalize([vector for _, vector in rows], dimensions),
                )
        finally:
            cursor.close()

    def _matrix(self, item: FieldVectors, count: int, dimensions: int) -> Resident | None:
        cached = self.matrices.get(item.values)
        if cached is not None:
            self.matrices.move_to_end(item.values)
            return cached
        size = count * (dimensions * 4 + 8)
        if size > self.matrix_budget:
            return None
        while (
            self.matrices
            and sum(matrix.size for matrix in self.matrices.values()) + size > self.matrix_budget
        ):
            self.matrices.popitem(last=False)
        rows = np.empty(count, dtype=np.int64)
        matrix = np.empty((count, dimensions), dtype=np.float32)
        start = 0
        for batch in self._batches(item, dimensions):
            stop = start + len(batch.rows)
            rows[start:stop], matrix[start:stop] = batch.rows, batch.matrix
            start = stop
        rows.flags.writeable = matrix.flags.writeable = False
        result = Resident(rows, matrix)
        self.matrices[item.values] = result
        return result

    def _resident_batches(self, resident: Resident) -> Iterator[Resident]:
        # Views keep the normalized allocation reusable while bounding dot/clip
        # output even for millions of very low-dimensional vectors.
        count = max(1, self.batch_budget // 16)
        for start in range(0, len(resident.rows), count):
            yield Resident(
                resident.rows[start : start + count], resident.matrix[start : start + count]
            )

    def score(self, field: str, query: str, destination: str) -> None:
        if self.identity is None or self.embed is None:
            raise QuailError(
                "Semantic search requires an embedding provider",
                "Configure embed and embed_revision for this dataset",
            )
        item = self._prepare(field)
        count, length = self.connection.execute(
            f"SELECT count(*),length(vec) FROM temp.{item.values}"
        ).fetchone()
        self.connection.execute(
            f"CREATE TABLE temp.{destination} (rowid INTEGER PRIMARY KEY, score REAL NOT NULL)"
        )
        if count == 0:
            return
        dimensions = int(length) // 4
        query_hash = digest_bytes(query.encode("utf-8"))
        stored = self.connection.execute(
            "SELECT vec FROM main.vectors WHERE embedding_id=? AND text_hash=?",
            (self.identity, query_hash),
        ).fetchone()
        vector = stored[0] if stored is not None else self._request([query])[0]
        normalized = normalize([vector], dimensions)
        resident = self._matrix(item, count, dimensions)
        batches = (
            self._resident_batches(resident)
            if resident is not None
            else self._batches(item, dimensions)
        )
        # Scores for distinct texts are temporary input to one set-based mapping.
        # Reuse this table across queries instead of retaining per-query vectors.
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS temp.semantic_scores "
            "(rowid INTEGER PRIMARY KEY, score REAL)"
        )
        self.connection.execute("DELETE FROM temp.semantic_scores")
        for batch in batches:
            scores = cosine(batch.matrix, normalized)
            self.connection.executemany(
                "INSERT INTO temp.semantic_scores VALUES (?,?)",
                (
                    (int(rowid), float(score))
                    for rowid, score in zip(batch.rows, scores, strict=True)
                ),
            )
        self.connection.execute(
            f"INSERT INTO temp.{destination} SELECT m.rowid,s.score FROM temp.{item.mapping} AS m "
            f"JOIN temp.{item.values} AS v ON v.text_hash=m.text_hash "
            "JOIN temp.semantic_scores AS s ON s.rowid=v.rowid"
        )
