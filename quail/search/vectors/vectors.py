"""Unit-vector packing helpers for the Turso vector cache."""

from __future__ import annotations

import hashlib
import math
import struct


def text_hash(text: str) -> str:
    """Stable SHA-256 of UTF-8 text for cache keys."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unit_vector(values: list[float]) -> list[float]:
    """L2-normalize a vector; zero vectors stay zero."""

    norm = math.sqrt(sum(component * component for component in values))
    if norm == 0.0:
        return [0.0 for _ in values]
    return [component / norm for component in values]


def pack_unit_vector(values: list[float]) -> bytes:
    """Pack a unit float32 vector as little-endian bytes for Turso."""

    return struct.pack(f"<{len(values)}f", *values)
