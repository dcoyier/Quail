"""Portable warm packs, a cohesive part of the derived-storage layer.

This module owns their schema, source inventories, atomic publication, and full
validation before cache insertion. It depends on the index's vector operations,
never on provider or kernel code. A pack is useful cache input, not study history.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import itertools
import math
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from quail.contracts import (
    JSONObject,
    QuailError,
    canonical_json,
    decode_json,
    digest_bytes,
    digest_json,
    json_object,
    sql_identifier,
    text_value,
)
from quail.index import Index, WarmPaths, transaction, validate_vector
from quail.project import EmbeddingConfig, sync_directory

WARN_PART_BYTES = 50 * 1024 * 1024
MAX_PART_BYTES = 100 * 1024 * 1024
_BATCH_BYTES = 4 * 1024 * 1024
type Progress = Callable[[str], None]


@dataclass(frozen=True)
class Shard:
    part: int
    total: int

    def __post_init__(self) -> None:
        if (
            type(self.part) is not int
            or type(self.total) is not int
            or not 1 <= self.part <= self.total
        ):
            raise QuailError("A shard must satisfy 1 <= I <= N", "Use --shard I/N, for example 1/4")

    @classmethod
    def parse(cls, value: str) -> Shard:
        if re.fullmatch(r"[0-9]+/[0-9]+", value) is None:
            raise QuailError("Invalid shard syntax", "Use --shard I/N, for example 1/4")
        return cls(*(int(piece) for piece in value.split("/")))

    def bounds(self, count: int) -> tuple[int, int]:
        return (self.part - 1) * count // self.total, self.part * count // self.total


@dataclass(frozen=True)
class Inventory:
    table: str
    fields: tuple[str, ...]
    count: int


@dataclass(frozen=True)
class Header:
    dataset: str
    source_version: str
    source_hash: str
    embedding: JSONObject
    fields: tuple[str, ...]
    dimensions: int
    shard: Shard

    @property
    def plan_hash(self) -> str:
        return digest_json(
            {
                "format": 1,
                "embedding_id": self.embedding["id"],
                "fields": list(self.fields),
                "shards": self.shard.total,
            }
        )

    @property
    def relative_path(self) -> Path:
        return Path(
            "warm",
            self.dataset,
            self.source_version.removeprefix("sha256:"),
            self.plan_hash.removeprefix("sha256:"),
            f"part-{self.shard.part:04d}-of-{self.shard.total:04d}.jsonl",
        )

    def to_record(self) -> JSONObject:
        return {
            "quail_warm": 1,
            "dataset": self.dataset,
            "source_version": self.source_version,
            "source_hash": self.source_hash,
            "embedding": self.embedding,
            "fields": list(self.fields),
            "dims": self.dimensions,
            "shard": [self.shard.part, self.shard.total],
        }

    def estimated_bytes(self, count: int) -> int:
        encoded_vector = 4 * ((self.dimensions * 4 + 2) // 3)
        empty_row = len(_line({"text_hash": "sha256:" + "0" * 64, "vector": ""}))
        return len(_line(self.to_record())) + count * (empty_row + encoded_vector)


def _line(value: JSONObject) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


class Packs:
    def __init__(self, index: Index, paths: WarmPaths) -> None:
        self.index, self.paths = index, paths
        self.connection = index.connection
        self.inventories: dict[tuple[str, ...], Inventory] = {}
        self.warnings: list[str] = []
        self._ingested = False

    def inventory(self, fields: tuple[str, ...]) -> Inventory:
        if (
            not fields
            or fields != tuple(sorted(set(fields)))
            or any(field not in self.index.source.fields[1:] for field in fields)
        ):
            raise QuailError("Warming requires sorted, distinct non-ID source fields")
        if fields in self.inventories:
            return self.inventories[fields]
        name = "warm_inventory_" + uuid.uuid4().hex
        self.connection.execute(
            f"CREATE TABLE temp.{name} "
            "(text_hash TEXT PRIMARY KEY, body TEXT NOT NULL) WITHOUT ROWID"
        )
        try:
            with transaction(self.connection):
                columns = ",".join(sql_identifier(field) for field in fields)
                cursor = self.connection.execute(f"SELECT {columns} FROM main.entries")
                try:
                    rendered = (text_value(value) for row in cursor for value in row)
                    records = (
                        (digest_bytes(text.encode("utf-8")), text) for text in rendered if text
                    )
                    for batch in itertools.batched(records, 256):
                        self.connection.executemany(
                            f"INSERT OR IGNORE INTO temp.{name} VALUES (?,?)", batch
                        )
                finally:
                    cursor.close()
            count = self.connection.execute(f"SELECT count(*) FROM temp.{name}").fetchone()[0]
            result = Inventory(name, fields, count)
            self.inventories[fields] = result
            return result
        except BaseException:
            self.connection.execute(f"DROP TABLE IF EXISTS temp.{name}")
            raise

    def rows(self, inventory: Inventory, shard: Shard) -> Generator[tuple[str, str], None, None]:
        start, stop = shard.bounds(inventory.count)
        cursor = self.connection.execute(
            f"SELECT text_hash,body FROM temp.{inventory.table} "
            "ORDER BY text_hash LIMIT ? OFFSET ?",
            (stop - start, start),
        )
        try:
            yield from cursor
        finally:
            cursor.close()

    def header(self, inventory: Inventory, config: EmbeddingConfig, shard: Shard) -> Header:
        dimensions = self.index.vector_dimensions(config.identity)
        if dimensions is None:
            raise QuailError("Warm dimensions have not been established")
        return Header(
            self.paths.dataset,
            self.index.source.version,
            self.index.source.hash,
            config.descriptor(),
            inventory.fields,
            dimensions,
            shard,
        )

    def check_size(self, header: Header, inventory: Inventory) -> int:
        start, stop = header.shard.bounds(inventory.count)
        estimated = header.estimated_bytes(stop - start)
        if estimated > MAX_PART_BYTES:
            row_bytes = header.estimated_bytes(1) - header.estimated_bytes(0)
            room = MAX_PART_BYTES - header.estimated_bytes(0) - 32
            suggested = max(
                header.shard.total + 1, math.ceil(inventory.count * row_bytes / max(1, room))
            )
            raise QuailError(
                f"Warm part would be {estimated} bytes, above the {MAX_PART_BYTES}-byte Git limit",
                f"Retry with more shards, for example --shard 1/{suggested}; "
                "local vectors remain cached",
            )
        if estimated > WARN_PART_BYTES:
            warning = f"Warm part is {estimated} bytes, above GitHub's 50 MiB warning threshold"
            if warning not in self.warnings:
                self.warnings.append(warning)
        return estimated

    def publish(self, header: Header, inventory: Inventory) -> tuple[Path, int]:
        self.check_size(header, inventory)
        destination = self.paths.root / header.relative_path
        if not destination.resolve().is_relative_to(self.paths.root):
            raise QuailError("Warm pack path leaves the project")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=".part-", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(_line(header.to_record()))
                size = max(1, min(256, _BATCH_BYTES // (header.dimensions * 4)))
                identity = str(header.embedding["id"])
                for batch in itertools.batched(self.rows(inventory, header.shard), size):
                    vectors = self.index.vectors(identity, [text_hash for text_hash, _ in batch])
                    if len(vectors) != len(batch):
                        raise QuailError("Warm cache is incomplete; no part was published")
                    for text_hash, _ in batch:
                        stream.write(
                            _line(
                                {
                                    "text_hash": text_hash,
                                    "vector": base64.b64encode(vectors[text_hash]).decode("ascii"),
                                }
                            )
                        )
                length = stream.tell()
                if length > MAX_PART_BYTES:
                    raise QuailError("Warm part exceeds the Git file limit; retry with more shards")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            sync_directory(destination.parent)
            return destination, length
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _header(self, record: JSONObject, config: EmbeddingConfig, relative: Path) -> Header:
        keys = {
            "quail_warm",
            "dataset",
            "source_version",
            "source_hash",
            "embedding",
            "fields",
            "dims",
            "shard",
        }
        if (
            record.keys() != keys
            or type(record["quail_warm"]) is not int
            or record["quail_warm"] != 1
        ):
            raise QuailError("Unsupported or malformed warm-pack header")
        fields, dims, shard = record["fields"], record["dims"], record["shard"]
        if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
            raise QuailError("Invalid warm-pack source fields")
        if type(dims) is not int or dims <= 0:
            raise QuailError("Invalid warm-pack dimensions")
        if (
            not isinstance(shard, list)
            or len(shard) != 2
            or any(type(part) is not int for part in shard)
        ):
            raise QuailError("Invalid warm-pack shard")
        assert isinstance(shard[0], int) and isinstance(shard[1], int)
        field_names = tuple(field for field in fields if isinstance(field, str))
        expected = Header(
            self.paths.dataset,
            self.index.source.version,
            self.index.source.hash,
            config.descriptor(),
            field_names,
            dims,
            Shard(shard[0], shard[1]),
        )
        if record != expected.to_record() or relative != expected.relative_path:
            raise QuailError(
                "Warm-pack path, source, or embedding descriptor does not match its header"
            )
        current = self.index.vector_dimensions(config.identity)
        if current is not None and current != dims:
            raise QuailError(f"Embedding dimensions changed: expected {current}, got {dims}")
        return expected

    def ingest(self, config: EmbeddingConfig, progress: Progress | None = None) -> None:
        if self._ingested:
            return
        for path in self.paths.files:
            try:
                inserted = self._ingest_file(path, config, progress)
                if inserted and progress is not None:
                    progress(
                        f"Ingested {inserted} shared vectors from "
                        f"{path.relative_to(self.paths.root)}"
                    )
            except (QuailError, OSError) as error:
                warning = f"Skipped warm pack {path}: {error}"
                self.warnings.append(warning)
                if progress is not None:
                    progress(warning)
        self._ingested = True

    def _ingest_file(self, path: Path, config: EmbeddingConfig, progress: Progress | None) -> int:
        if not path.resolve().is_relative_to(self.paths.root):
            raise QuailError("Warm pack path leaves the project")
        relative = path.relative_to(self.paths.root)
        with path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > MAX_PART_BYTES:
                raise QuailError("Warm pack exceeds the Git file limit")
            receipt = self.index.ingested(relative.as_posix())
            if receipt is not None:
                current = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
                if current == receipt:
                    return 0
                stream.seek(0)
            first = stream.readline(MAX_PART_BYTES + 1)
            if not first.endswith(b"\n"):
                raise QuailError("Warm header is incomplete")
            record = json_object(decode_json(first), "warm header")
            descriptor = record.get("embedding")
            if isinstance(descriptor, dict) and descriptor.get("id") != config.identity:
                return 0  # Other revisions/configurations are ordinary neighboring packs.
            header = self._header(record, config, relative)
            inventory = self.inventory(header.fields)
            name = "warm_stage_" + uuid.uuid4().hex
            self.connection.execute(
                f"CREATE TABLE temp.{name} "
                "(text_hash TEXT PRIMARY KEY, vec BLOB NOT NULL) WITHOUT ROWID"
            )
            try:
                digest = self._stage(stream, first, name, header, inventory, progress)
                count = self._insert(name, header, (relative.as_posix(), digest))
                return count
            finally:
                self.connection.execute(f"DROP TABLE IF EXISTS temp.{name}")

    def _stage(
        self,
        stream: BinaryIO,
        first: bytes,
        table: str,
        header: Header,
        inventory: Inventory,
        progress: Progress | None,
    ) -> str:
        digest = hashlib.sha256(first)
        expected = self.rows(inventory, header.shard)
        count = 0
        try:
            with transaction(self.connection):
                while line := stream.readline(MAX_PART_BYTES + 1):
                    count += 1
                    digest.update(line)
                    if not line.endswith(b"\n"):
                        raise QuailError(f"Warm vector line {count + 1} is incomplete")
                    record = json_object(decode_json(line), "warm vector")
                    wanted = next(expected, None)
                    if (
                        record.keys() != {"text_hash", "vector"}
                        or wanted is None
                        or record["text_hash"] != wanted[0]
                    ):
                        raise QuailError(
                            "Warm hashes do not exactly cover the declared range "
                            f"at line {count + 1}"
                        )
                    vector = record["vector"]
                    try:
                        if not isinstance(vector, str):
                            raise ValueError("Vector must be base64 text")
                        packed = base64.b64decode(vector, validate=True)
                        if base64.b64encode(packed).decode("ascii") != vector:
                            raise ValueError("Vector is not canonical base64")
                    except (ValueError, binascii.Error) as error:
                        raise QuailError(
                            f"Invalid warm vector at line {count + 1}: {error}"
                        ) from error
                    validate_vector(packed, header.dimensions)
                    self.connection.execute(
                        f"INSERT INTO temp.{table} VALUES (?,?)", (wanted[0], packed)
                    )
                    if count % 128 == 0 and progress is not None:
                        progress(f"Validating shared pack: {count} vectors")
                if next(expected, None) is not None:
                    raise QuailError("Warm pack is missing vectors from its declared range")
            return "sha256:" + digest.hexdigest()
        finally:
            expected.close()

    def _insert(self, table: str, header: Header, receipt: tuple[str, str]) -> int:
        size = max(1, min(256, _BATCH_BYTES // (header.dimensions * 4)))
        cursor = self.connection.execute(
            f"SELECT text_hash,vec FROM temp.{table} ORDER BY text_hash"
        )
        inserted = 0
        try:
            batch = cursor.fetchmany(size)
            while True:
                following = cursor.fetchmany(size)
                result = self.index.insert_vectors(
                    str(header.embedding["id"]),
                    batch,
                    receipt=receipt if not following else None,
                )
                inserted += result.inserted
                if not following:
                    return inserted
                batch = following
        finally:
            cursor.close()
