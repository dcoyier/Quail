"""Shared values and records, independent of storage and process ownership.

The host and child use the same JSON rules. Decoding establishes only the value
domain; the operation receiving a record must still validate its schema and scope.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import NoReturn

type JSONScalar = None | bool | int | float | str
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]
type TagDelta = dict[str, dict[str, JSONValue]]


class QuailError(Exception):
    """An expected, actionable failure at a Quail operation boundary."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def json_value(value: object) -> JSONValue:
    """Validate and snapshot an ordinary Python JSON value without coercion.

    Returning new containers prevents later Python mutation from changing an
    expression literal, a staged annotation, or a record awaiting publication.
    """
    try:
        return _json_value(value)
    except RecursionError as error:
        raise QuailError("JSON values cannot be recursive or excessively nested") from error


def _json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QuailError("JSON numbers must be finite")
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise QuailError("JSON strings must be encodable as UTF-8") from error
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        result: JSONObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise QuailError("JSON object keys must be strings")
            _json_value(key)
            result[key] = _json_value(item)
        return result
    raise QuailError(f"Expected a JSON value, got {type(value).__name__}")


def canonical_json(value: JSONValue) -> str:
    """One byte spelling for descriptors, object text, and stored annotations."""
    # Validation is also necessary for strings, keys, and encoder-coercible types.
    checked = json_value(value)
    return json.dumps(
        checked, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _reject_constant(token: str) -> NoReturn:
    raise QuailError(f"Invalid JSON number: {token}")


def _object_pairs(pairs: list[tuple[str, JSONValue]]) -> JSONObject:
    result: JSONObject = {}
    for key, value in pairs:
        if key in result:
            raise QuailError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def decode_json(text: str | bytes) -> JSONValue:
    """Read strict UTF-8 JSON, retaining booleans and numeric spellings as types."""
    try:
        source = text.decode("utf-8") if isinstance(text, bytes) else text
        decoded: object = json.loads(
            source, parse_constant=_reject_constant, object_pairs_hook=_object_pairs
        )
        return json_value(decoded)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise QuailError(f"Invalid JSON: {error}") from error


def json_object(value: JSONValue, label: str = "record") -> JSONObject:
    if not isinstance(value, dict):
        raise QuailError(f"{label} must be an object")
    return value


def text_value(value: JSONValue) -> str | None:
    """Render stored values once, identically for language operations and warming."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join("null" if item is None else text_value(item) or "" for item in value)
    return canonical_json(value)


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_json(value: JSONValue) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def source_version(source_hash: str, id_column: str | None) -> str:
    return digest_json({"import_format": 1, "source_hash": source_hash, "id_column": id_column})


def embedding_identity(embed: str, revision: str) -> str:
    return digest_json({"format": 1, "embed": embed, "revision": revision})


def sql_identifier(name: str) -> str:
    """Quote an identifier; values always use bound parameters instead."""
    if not name or "\0" in name:
        raise QuailError("SQL identifiers must be non-empty and contain no NUL")
    return '"' + name.replace('"', '""') + '"'


def fts_table(field: str) -> str:
    # FTS5 derives unquoted shadow-table names, so a letter prefix is essential.
    return "fts_" + hashlib.sha256(field.encode("utf-8")).hexdigest()


def checked_hash(value: JSONValue, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise QuailError(f"Invalid {label}: expected a SHA-256 identity")
    return value


@dataclass(frozen=True)
class Source:
    """The immutable source identity and catalog shared by host and child."""

    hash: str
    version: str
    id_column: str | None
    fields: tuple[str, ...]
    present: tuple[int, ...]
    rows: int

    def to_record(self) -> JSONObject:
        return {
            "hash": self.hash,
            "version": self.version,
            "id_column": self.id_column,
            "fields": list(self.fields),
            "present": list(self.present),
            "rows": self.rows,
        }

    @classmethod
    def from_record(cls, record: JSONObject) -> Source:
        source_hash = checked_hash(record.get("hash"), "source hash")
        version = checked_hash(record.get("version"), "source version")
        id_column = record.get("id_column")
        fields, present, rows = record.get("fields"), record.get("present"), record.get("rows")
        if id_column is not None and not isinstance(id_column, str):
            raise QuailError("Invalid source ID column")
        if version != source_version(source_hash, id_column):
            raise QuailError("Invalid source identity")
        if not isinstance(fields, list) or not all(isinstance(item, str) for item in fields):
            raise QuailError("Invalid source fields")
        if (
            not isinstance(present, list)
            or len(present) != len(fields)
            or any(type(item) is not int or item < 0 for item in present)
            or type(rows) is not int
            or rows < 0
        ):
            raise QuailError("Invalid source counts")
        return cls(
            source_hash,
            version,
            id_column,
            tuple(item for item in fields if isinstance(item, str)),
            tuple(item for item in present if isinstance(item, int)),
            rows,
        )


@dataclass(frozen=True)
class Limits:
    """Resolved settings shared with the child and reported in execution results."""

    cpu_seconds: float = 30
    wall_seconds: float = 120
    memory_mb: int = 1024
    max_limit: int = 1000
    output_kib: int = 64

    def to_record(self) -> JSONObject:
        return {
            "cpu_seconds": self.cpu_seconds,
            "wall_seconds": self.wall_seconds,
            "memory_mb": self.memory_mb,
            "max_limit": self.max_limit,
            "output_kib": self.output_kib,
        }

    @classmethod
    def from_record(cls, record: JSONObject) -> Limits:
        defaults = cls().to_record()
        unknown = record.keys() - defaults.keys()
        if unknown:
            raise QuailError(f"Unknown kernel settings: {', '.join(sorted(unknown))}")
        values = defaults | record
        numbers: dict[str, int | float] = {}
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise QuailError(f"{key} must be a number")
            if (
                (isinstance(value, float) and not math.isfinite(value))
                or value < 0
                or (key != "max_limit" and value == 0)
            ):
                raise QuailError(f"Invalid {key}: {value}")
            if key not in {"cpu_seconds", "wall_seconds"} and not isinstance(value, int):
                raise QuailError(f"{key} must be an integer")
            numbers[key] = value
        return cls(
            cpu_seconds=float(numbers["cpu_seconds"]),
            wall_seconds=float(numbers["wall_seconds"]),
            memory_mb=int(numbers["memory_mb"]),
            max_limit=int(numbers["max_limit"]),
            output_kib=int(numbers["output_kib"]),
        )


@dataclass(frozen=True)
class ErrorInfo:
    type: str
    message: str
    hint: str | None = None

    @classmethod
    def from_exception(cls, error: BaseException) -> ErrorInfo:
        return cls(
            "QuailError" if isinstance(error, QuailError) else type(error).__name__,
            str(error),
            error.hint if isinstance(error, QuailError) else None,
        )

    def to_record(self) -> JSONObject:
        return {"type": self.type, "message": self.message, "hint": self.hint}

    @classmethod
    def from_record(cls, record: JSONObject) -> ErrorInfo:
        name, message, hint = record.get("type"), record.get("message"), record.get("hint")
        if not isinstance(name, str) or not isinstance(message, str):
            raise QuailError("Invalid error type or message")
        if hint is not None and not isinstance(hint, str):
            raise QuailError("Invalid error hint")
        if record.keys() != {"type", "message", "hint"}:
            raise QuailError("Invalid error fields")
        return cls(name, message, hint)


def tag_delta(value: JSONValue) -> TagDelta:
    """Validate the shared delta shape; its receiver still checks live scope."""
    result: TagDelta = {}
    for name, entries in json_object(value, "tags").items():
        if not name or "\0" in name:
            raise QuailError("Invalid tag field name")
        result[name] = json_object(entries, "tag entries")
        if any(not entry for entry in result[name]):
            raise QuailError("Invalid tag entry ID")
    return result


@dataclass(frozen=True)
class CellReply:
    n: int
    output: str
    error: ErrorInfo | None
    truncated: bool
    tags: TagDelta

    def to_record(self) -> JSONObject:
        return {
            "type": "result",
            "n": self.n,
            "output": self.output,
            "error": self.error.to_record() if self.error else None,
            "truncated": self.truncated,
            "tags": {name: entries for name, entries in self.tags.items()},
        }

    @classmethod
    def from_record(cls, record: JSONObject, expected: int) -> CellReply:
        if record.keys() != {"type", "n", "output", "error", "truncated", "tags"}:
            raise QuailError("Invalid child result fields")
        if record["type"] != "result" or type(record["n"]) is not int or record["n"] != expected:
            raise QuailError("Child result does not match the accepted cell")
        output, truncated = record["output"], record["truncated"]
        if not isinstance(output, str) or not isinstance(truncated, bool):
            raise QuailError("Invalid child output")
        error_value = record["error"]
        error = ErrorInfo.from_record(json_object(error_value)) if error_value is not None else None
        tags = tag_delta(record["tags"])
        if error is not None and tags:
            raise QuailError("A failed cell cannot write tags")
        return cls(expected, output, error, truncated, tags)


@dataclass(frozen=True)
class Execution:
    session: str
    run: str
    reply: CellReply
    limits: Limits
    warnings: tuple[str, ...] = ()
    kernel_restarted: bool = False

    @property
    def cell(self) -> int:
        return self.reply.n

    def to_record(self) -> JSONObject:
        return {
            "session": self.session,
            "run": self.run,
            "cell": self.cell,
            "output": self.reply.output,
            "error": self.reply.error.to_record() if self.reply.error else None,
            "tags_written": sum(map(len, self.reply.tags.values())),
            "truncated": self.reply.truncated,
            "kernel_restarted": self.kernel_restarted,
            "warnings": list(self.warnings),
            "limits": self.limits.to_record(),
        }
