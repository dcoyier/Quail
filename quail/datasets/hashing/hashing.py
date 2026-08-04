"""Canonical JSON and dataset content hashing."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from quail.datasets.errors import DatasetSyntaxError

_MAX_INT_DIGITS = 4096


def canonical_json(value: Any) -> str:
    """Stable JSON text for hashing and durable source values."""

    return json.dumps(
        _normalize_json(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_json(text: str) -> Any:
    return _normalize_json(json.loads(text))


def value_hash_from_canonical_json(encoded: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"quail:value:v1\0")
    digest.update(encoded.encode("utf-8"))
    return digest.hexdigest()


def dataset_content_hash(
    entries: Iterable[Mapping[str, Any]],
    fields: Sequence[str],
) -> str:
    """Hash every behaviorally relevant part of one immutable source version.

    ``entries`` may be a one-shot iterator so large imports do not need to
    materialize the whole dataset before computing its immutable identity.
    """

    digest = hashlib.sha256()
    digest.update(b'quail:dataset:v1\0{"entries":[')
    for position, entry in enumerate(entries):
        if position:
            digest.update(b",")
        digest.update(canonical_json(dict(entry)).encode("utf-8"))
    digest.update(b'],"fields":')
    digest.update(canonical_json(list(fields)).encode("utf-8"))
    digest.update(b"}")
    return digest.hexdigest()


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) >= 10**_MAX_INT_DIGITS:
            raise DatasetSyntaxError(f"Integers cannot exceed {_MAX_INT_DIGITS} decimal digits")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DatasetSyntaxError("Non-finite floats are not Quail values")
        return value
    if isinstance(value, list | tuple):
        return [_normalize_json(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise DatasetSyntaxError("Quail object keys must be strings")
            normalized[key] = _normalize_json(item)
        return normalized
    raise DatasetSyntaxError(f"Unsupported Quail value type: {type(value).__name__}")
