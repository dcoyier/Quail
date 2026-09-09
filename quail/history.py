"""Append-only run records and deterministic, streaming history validation.

History owns the durable truth. It yields ordered changes but never writes a
database: callers stage the changes and publish only after iteration succeeds.
"""

from __future__ import annotations

import hashlib
import heapq
import os
import uuid
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from quail.contracts import (
    ErrorInfo,
    JSONObject,
    JSONValue,
    QuailError,
    TagDelta,
    canonical_json,
    checked_hash,
    decode_json,
    digest_json,
    embedding_identity,
    json_object,
    source_version,
)
from quail.project import SessionMetadata, sync_directory, validate_name


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(record: JSONObject, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise QuailError(f"{key} must be text")
    return value


def _integer(record: JSONObject, key: str, *, minimum: int = 0) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise QuailError(f"{key} must be an integer >= {minimum}")
    return value


def _timestamp(record: JSONObject, key: str) -> str:
    value = _text(record, key)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise QuailError(f"{key} must be a UTC timestamp") from error
    if parsed.utcoffset() != timedelta(0):
        raise QuailError(f"{key} must be a UTC timestamp")
    return value


def _keys(record: JSONObject, expected: set[str]) -> None:
    if record.keys() != expected:
        raise QuailError(
            f"Invalid record fields; missing={sorted(expected - record.keys())}, "
            f"extra={sorted(record.keys() - expected)}"
        )


@dataclass(frozen=True)
class RunHeader:
    run: str
    started: str
    actor: str
    quail: str
    dataset: str
    source_hash: str
    source_version: str
    id_column: str | None
    embedding: JSONObject | None
    confinement: str

    def to_record(self) -> JSONObject:
        return {
            "format": 1,
            "run": self.run,
            "started": self.started,
            "actor": self.actor,
            "quail": self.quail,
            "dataset": self.dataset,
            "source_hash": self.source_hash,
            "source_version": self.source_version,
            "id_column": self.id_column,
            "embedding": self.embedding,
            "confinement": self.confinement,
        }

    @classmethod
    def from_record(cls, record: JSONObject, session: SessionMetadata, run: str) -> RunHeader:
        _keys(
            record,
            {
                "format",
                "run",
                "started",
                "actor",
                "quail",
                "dataset",
                "source_hash",
                "source_version",
                "id_column",
                "embedding",
                "confinement",
            },
        )
        if _integer(record, "format") != 1:
            raise QuailError("Unsupported run-log format")
        if _text(record, "run") != run:
            raise QuailError("Run identity does not match its filename")
        validate_name(run, "run")
        if _text(record, "dataset") != session.dataset:
            raise QuailError("Run belongs to another dataset")
        id_column = record["id_column"]
        if id_column is not None and (not isinstance(id_column, str) or not id_column):
            raise QuailError("Invalid run ID column")
        source_hash = checked_hash(record["source_hash"], "source hash")
        version = checked_hash(record["source_version"], "source version")
        if version != source_version(source_hash, id_column):
            raise QuailError("Run source-version descriptor does not match its hash")
        if id_column is None and (
            session.id_column is not None or version != session.source_version
        ):
            raise QuailError("Generated IDs do not belong to this session's original source")
        embedding_value = record["embedding"]
        embedding = None
        if embedding_value is not None:
            embedding = json_object(embedding_value, "embedding descriptor")
            _keys(embedding, {"id", "embed", "revision"})
            if embedding.get("id") != embedding_identity(
                _text(embedding, "embed"), _text(embedding, "revision")
            ):
                raise QuailError("Invalid embedding identity")
        confinement = _text(record, "confinement")
        if confinement not in {"audit", "audit+netns"}:
            raise QuailError("Invalid confinement mode")
        return cls(
            run=run,
            started=_timestamp(record, "started"),
            actor=_text(record, "actor"),
            quail=_text(record, "quail"),
            dataset=session.dataset,
            source_hash=source_hash,
            source_version=version,
            id_column=id_column,
            embedding=embedding,
            confinement=confinement,
        )


def tag_delta(value: JSONValue) -> TagDelta:
    result: TagDelta = {}
    for name, entries in json_object(value, "tags").items():
        if not name or "\0" in name:
            raise QuailError("Invalid tag field name")
        result[name] = json_object(entries, "tag entries")
        if any(not entry for entry in result[name]):
            raise QuailError("Invalid tag entry ID")
    return result


@dataclass(frozen=True)
class CellRecord:
    n: int
    order: int
    started: str
    ended: str
    code: str
    output: str
    error: ErrorInfo | None
    truncated: bool
    tags: TagDelta

    @property
    def tags_written(self) -> int:
        return sum(len(entries) for entries in self.tags.values())

    def to_record(self) -> JSONObject:
        return {
            "n": self.n,
            "order": self.order,
            "started": self.started,
            "ended": self.ended,
            "code": self.code,
            "output": self.output,
            "error": self.error.to_record() if self.error else None,
            "truncated": self.truncated,
            "tags_written": self.tags_written,
            "tags": {name: entries for name, entries in self.tags.items()},
        }

    @classmethod
    def from_record(cls, record: JSONObject) -> CellRecord:
        _keys(
            record,
            {
                "n",
                "order",
                "started",
                "ended",
                "code",
                "output",
                "error",
                "truncated",
                "tags_written",
                "tags",
            },
        )
        error_value = record["error"]
        error = ErrorInfo.from_record(json_object(error_value)) if error_value is not None else None
        tags = tag_delta(record["tags"])
        if error is not None and tags:
            raise QuailError("A failed cell cannot contain tag writes")
        if _integer(record, "tags_written") != sum(map(len, tags.values())):
            raise QuailError("tags_written does not match the tag delta")
        truncated = record["truncated"]
        if not isinstance(truncated, bool):
            raise QuailError("truncated must be boolean")
        return cls(
            n=_integer(record, "n", minimum=1),
            order=_integer(record, "order", minimum=1),
            started=_timestamp(record, "started"),
            ended=_timestamp(record, "ended"),
            code=_text(record, "code"),
            output=_text(record, "output"),
            error=error,
            truncated=truncated,
            tags=tags,
        )


@dataclass(frozen=True)
class LogFile:
    path: Path
    digest: str


@dataclass(frozen=True)
class Snapshot:
    files: tuple[LogFile, ...]

    @property
    def hashes(self) -> dict[str, str]:
        return {item.path.name: item.digest for item in self.files}

    @property
    def digest(self) -> str:
        return history_digest(self.hashes)


def history_digest(hashes: Mapping[str, str]) -> str:
    return digest_json([[name, digest] for name, digest in sorted(hashes.items())])


def snapshot(directory: Path) -> Snapshot:
    files = []
    for path in sorted(directory.glob("*.jsonl")):
        if not path.resolve().is_relative_to(directory.resolve()):
            raise QuailError(f"Run log escapes its session: {path}")
        try:
            with path.open("rb") as stream:
                digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as error:
            raise QuailError(f"Cannot read log {path}: {error}") from error
        files.append(LogFile(path, digest))
    return Snapshot(tuple(files))


@dataclass
class Summary:
    runs: int = 0
    cells: int = 0
    failed: int = 0
    max_order: int = 0
    last_activity: str | None = None
    last_source_version: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_record(self) -> JSONObject:
        return {
            "runs": self.runs,
            "cells": self.cells,
            "failed": self.failed,
            "max_order": self.max_order,
            "last_activity": self.last_activity,
            "last_source_version": self.last_source_version,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_record(cls, record: JSONObject) -> Summary:
        warnings = record.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise QuailError("Invalid cached history warnings")
        activity, version = record.get("last_activity"), record.get("last_source_version")
        if activity is not None and not isinstance(activity, str):
            raise QuailError("Invalid cached activity")
        if version is not None and not isinstance(version, str):
            raise QuailError("Invalid cached source version")
        return cls(
            runs=_integer(record, "runs"),
            cells=_integer(record, "cells"),
            failed=_integer(record, "failed"),
            max_order=_integer(record, "max_order"),
            last_activity=activity,
            last_source_version=version,
            warnings=[item for item in warnings if isinstance(item, str)],
        )

    def completed(self, header: RunHeader, cell: CellRecord) -> None:
        self.cells += 1
        self.failed += cell.error is not None
        self.max_order = max(self.max_order, cell.order)
        self.last_activity = max(self.last_activity or "", cell.ended)
        # Callers deliver cells in replay order, or append after all observed history.
        self.last_source_version = header.source_version


def records(
    history: Snapshot, session: SessionMetadata, summary: Summary
) -> Iterator[tuple[RunHeader, CellRecord]]:
    """Yield complete records in logical order; a later invalid line still raises."""
    streams = [_run_records(item, session, summary) for item in history.files]
    ordered = heapq.merge(*streams, key=lambda pair: (pair[1].order, pair[0].run, pair[1].n))
    try:
        for header, cell in ordered:
            summary.completed(header, cell)
            yield header, cell
    finally:
        # Release descriptors even if the consumer's private staging fails midway.
        for stream in streams:
            stream.close()


def _run_records(
    item: LogFile, session: SessionMetadata, summary: Summary
) -> Generator[tuple[RunHeader, CellRecord], None, None]:
    line_number = 0
    hasher = hashlib.sha256()
    try:
        with item.path.open("rb") as stream:
            header: RunHeader | None = None
            previous_order = 0
            for line_number, raw in enumerate(stream, 1):
                hasher.update(raw)
                if not raw.endswith(b"\n"):
                    kind = "run creation" if header is None else "append"
                    summary.warnings.append(
                        f"Ignored interrupted {kind}: {item.path.name}:{line_number}"
                    )
                    break
                value = json_object(decode_json(raw))
                if header is None:
                    header = RunHeader.from_record(value, session, item.path.stem)
                    summary.runs += 1
                    continue
                cell = CellRecord.from_record(value)
                if cell.n != line_number - 1 or cell.order <= previous_order:
                    raise QuailError(
                        "Cell numbering must be contiguous and logical order increasing"
                    )
                previous_order = cell.order
                yield header, cell
            if line_number == 0:
                summary.warnings.append(f"Ignored interrupted run creation: {item.path.name}:1")
            if "sha256:" + hasher.hexdigest() != item.digest:
                raise QuailError("History changed while it was being read; close writers and retry")
    except (OSError, QuailError) as error:
        raise QuailError(
            f"Session {session.name!r}, {item.path.name}:{line_number}: {error}"
        ) from error


class RunLog:
    """An exclusively created run file; uncertain writes permanently poison its handle."""

    def __init__(self, directory: Path, header: RunHeader) -> None:
        self.header = header
        self.path = directory / f"{header.run}.jsonl"
        self._hash = hashlib.sha256()
        self._next_cell = 1
        self._last_order = 0
        self._failed = False
        directory.mkdir(parents=True, exist_ok=True)
        self._stream: BinaryIO = self.path.open("xb")
        try:
            self._write(header.to_record())
            sync_directory(directory)
        except BaseException:
            self._stream.close()
            raise

    @staticmethod
    def new_id() -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{stamp}-{uuid.uuid4()}"

    @property
    def digest(self) -> str:
        return "sha256:" + self._hash.hexdigest()

    def append(self, cell: CellRecord) -> None:
        if cell.n != self._next_cell or cell.order <= self._last_order:
            raise QuailError("Invalid cell sequence for this run")
        self._write(cell.to_record())
        self._next_cell += 1
        self._last_order = cell.order

    def _write(self, record: JSONObject) -> None:
        if self._failed:
            raise QuailError("This run log has an uncertain append and cannot continue")
        payload = (canonical_json(record) + "\n").encode("utf-8")
        try:
            self._stream.write(payload)
            self._stream.flush()
            os.fsync(self._stream.fileno())
        except BaseException:
            self._failed = True
            raise
        self._hash.update(payload)

    def close(self) -> None:
        self._stream.close()
