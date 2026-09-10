"""Small framed JSON transport shared by private pipes and local connections.

Framing establishes a bounded complete object, never an operation's validity or
scope. The receiver validates the record before accepting work. EOF is distinct
from an empty record and incomplete frames never become submitted cells.
"""

from __future__ import annotations

import struct
from collections.abc import Buffer
from typing import Protocol

from quail.contracts import JSONObject, QuailError, canonical_json, decode_json, json_object

DEFAULT_MAX_FRAME = 64 * 1024 * 1024


class Reader(Protocol):
    def read(self, count: int, /) -> bytes | None: ...


class Writer(Protocol):
    def write(self, data: Buffer, /) -> int | None: ...
    def flush(self) -> None: ...


def encode(record: JSONObject, max_bytes: int = DEFAULT_MAX_FRAME) -> bytes:
    payload = canonical_json(record).encode("utf-8")
    if len(payload) > max_bytes:
        raise QuailError(f"Control record exceeds its {max_bytes}-byte memory allowance")
    return struct.pack("!I", len(payload)) + payload


def send(stream: Writer, record: JSONObject, max_bytes: int = DEFAULT_MAX_FRAME) -> None:
    payload = memoryview(encode(record, max_bytes))
    while payload:
        written = stream.write(payload)
        if written is None or written <= 0:
            raise BrokenPipeError("Control channel closed during write")
        payload = payload[written:]
    stream.flush()


def _exact(stream: Reader, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        part = stream.read(count - len(result))
        if not part:
            raise EOFError("Control channel ended before a complete frame")
        result.extend(part)
    return bytes(result)


def receive(stream: Reader, max_bytes: int = DEFAULT_MAX_FRAME) -> JSONObject:
    size = struct.unpack("!I", _exact(stream, 4))[0]
    if size > max_bytes:
        raise QuailError(f"Control frame exceeds its {max_bytes}-byte memory allowance")
    return json_object(decode_json(_exact(stream, size)), "control record")


class Decoder:
    """Incremental receive state for a host that must keep monitoring its child."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_FRAME) -> None:
        self.max_bytes = max_bytes
        self._buffer = bytearray()
        self._size: int | None = None

    def feed(self, data: bytes) -> list[JSONObject]:
        self._buffer.extend(data)
        result = []
        while True:
            if self._size is None:
                if len(self._buffer) < 4:
                    break
                self._size = struct.unpack("!I", self._buffer[:4])[0]
                del self._buffer[:4]
                if self._size > self.max_bytes:
                    raise QuailError("Control frame exceeds its memory allowance")
            if len(self._buffer) < self._size:
                break
            payload = bytes(self._buffer[: self._size])
            del self._buffer[: self._size]
            self._size = None
            result.append(json_object(decode_json(payload), "control record"))
        return result
