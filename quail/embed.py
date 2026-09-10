"""Two HTTP dialects and one cache path for kernels and explicit warming.

Raw provider calls perform no SQLite work. The synchronous caller owns the cache
and can place just the raw call on a worker while continuing kernel monitoring.
Credentials are resolved only for HTTP misses, never during orientation or hits.
"""

from __future__ import annotations

import http.client
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import cast

from quail.contracts import (
    EMBED_TEXT_BYTES,
    EMBED_TEXT_ITEMS,
    JSONObject,
    JSONValue,
    QuailError,
    canonical_json,
    decode_json,
    digest_bytes,
    json_object,
)
from quail.index import Index, pack_vector
from quail.packs import Packs
from quail.project import EmbeddingConfig

type RawEmbed = Callable[[EmbeddingConfig, list[str]], list[list[float]]]
type Progress = Callable[[str], None]

MAX_BATCH_ITEMS = EMBED_TEXT_ITEMS
MAX_BATCH_BYTES = EMBED_TEXT_BYTES
REQUEST_TIMEOUT = 15
ATTEMPT_SECONDS = 30
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
ATTEMPTS = 3


def _texts_batched(texts: Sequence[str]) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for text in texts:
        encoded = len(canonical_json(text).encode("utf-8"))
        if batch and (len(batch) >= MAX_BATCH_ITEMS or size + encoded > MAX_BATCH_BYTES):
            yield batch
            batch, size = [], 0
        # One complete value is indivisible. A larger individual input goes alone;
        # the provider can reject it, but Quail never truncates or splits it.
        batch.append(text)
        size += encoded
    if batch:
        yield batch


def _response(response: http.client.HTTPResponse, deadline: float) -> bytes:
    content = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("Embedding provider exceeded the attempt deadline")
        chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(content)))
        if not chunk:
            return bytes(content)
        content.extend(chunk)
        if len(content) > MAX_RESPONSE_BYTES:
            raise QuailError("Embedding response exceeds the bounded provider batch allowance")


def _http_error(error: urllib.error.HTTPError, credential: str | None) -> QuailError:
    message = f"Embedding provider rejected the request (HTTP {error.code})"
    try:
        details = json_object(decode_json(error.read(8192))).get("error")
        if isinstance(details, dict):
            details = details.get("message")
        if isinstance(details, str):
            message += ": " + details[:2048]
    except (OSError, QuailError):
        pass
    if credential:
        message = message.replace(credential, "[redacted]")
    return QuailError(message, "Check the provider, model, credentials, and complete input size")


def _decode_vectors(value: JSONValue, count: int) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != count:
        raise QuailError("Provider response count does not match the submitted texts")
    result = []
    for row in value:
        if not isinstance(row, list) or any(type(item) not in {int, float} for item in row):
            raise QuailError("Provider embeddings must be arrays of numbers")
        try:
            result.append([float(item) for item in row if isinstance(item, (int, float))])
        except OverflowError as error:
            raise QuailError("Provider coordinate exceeds the finite number range") from error
    return result


def provider(config: EmbeddingConfig, texts: list[str]) -> list[list[float]]:
    """The raw, bounded HTTP call; validation/storage remain with the cache owner."""
    body: JSONObject = {"model": config.model, "input": list(texts)}
    if config.dialect == "ollama":
        path = "/api/embed"
        body["truncate"] = False
    else:
        path = "/embeddings"
        body["encoding_format"] = "float"
    credential = os.environ.get(config.key_env) if config.key_env is not None else None
    if config.key_env is not None and not credential:
        raise QuailError(f"Provider credential environment variable is unset: {config.key_env}")
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = "Bearer " + credential
    request = urllib.request.Request(
        config.base_url + path,
        canonical_json(body).encode("utf-8"),
        headers,
        method="POST",
    )
    response_bytes = b""
    for attempt in range(ATTEMPTS):
        deadline = time.monotonic() + ATTEMPT_SECONDS
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                response_bytes = _response(cast(http.client.HTTPResponse, response), deadline)
            break
        except urllib.error.HTTPError as error:
            try:
                if attempt + 1 == ATTEMPTS or (error.code != 429 and not 500 <= error.code < 600):
                    raise _http_error(error, credential) from error
            finally:
                error.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            if attempt + 1 == ATTEMPTS:
                raise QuailError(
                    "Embedding provider could not complete a bounded request", str(error)
                ) from error
    document = json_object(decode_json(response_bytes), "embedding response")
    if config.dialect == "ollama":
        return _decode_vectors(document.get("embeddings"), len(texts))
    data = document.get("data")
    if not isinstance(data, list) or len(data) != len(texts):
        raise QuailError("Provider response count does not match the submitted texts")
    ordered: list[JSONValue] = [None] * len(texts)
    observed: set[int] = set()
    for item in data:
        record = json_object(item, "embedding item")
        position = record.get("index")
        if type(position) is not int or not 0 <= position < len(texts) or position in observed:
            raise QuailError("Invalid or duplicate embedding response index")
        observed.add(position)
        ordered[position] = record.get("embedding")
    return _decode_vectors(ordered, len(texts))


@dataclass(frozen=True)
class Embedded:
    vectors: tuple[bytes, ...]
    reused: int
    created: int


class Cache:
    """One operation's cache coordinator, with raw HTTP as its sole substitution."""

    def __init__(
        self,
        index: Index,
        config: EmbeddingConfig,
        *,
        raw: RawEmbed | None = None,
        progress: Progress | None = None,
    ) -> None:
        self.index, self.config = index, config
        self.raw = raw or provider
        self.progress = progress
        self.packs = Packs(index, index.warm_paths) if index.warm_paths is not None else None

    def get(self, texts: Sequence[str]) -> Embedded:
        # Callers pass bounded working batches, not a Python list of the corpus.
        if self.packs is not None:
            self.packs.ingest(self.config, self.progress)
        unique: dict[str, str] = {}
        order = []
        for text in texts:
            if not isinstance(text, str) or not text:
                raise QuailError("Embedding requests require non-empty complete text")
            text_hash = digest_bytes(text.encode("utf-8"))
            unique.setdefault(text_hash, text)
            order.append(text_hash)
        stored = self.index.vectors(self.config.identity, list(unique))
        reused = len(stored)
        missing = [text for text_hash, text in unique.items() if text_hash not in stored]
        created = 0
        for batch in _texts_batched(missing):
            if self.progress is not None:
                self.progress(
                    f"Embedding {len(batch)} values: {reused} reused, {created} new so far"
                )
            raw = self.raw(self.config, batch)
            if len(raw) != len(batch):
                raise QuailError("Provider response count does not match the submitted texts")
            rows = [
                (digest_bytes(text.encode("utf-8")), pack_vector(vector))
                for text, vector in zip(batch, raw, strict=True)
            ]
            canonical = self.index.insert_vectors(self.config.identity, rows)
            stored.update(
                (text_hash, vector)
                for (text_hash, _), vector in zip(rows, canonical.vectors, strict=True)
            )
            # Count provider work even if another writer won the same cache key.
            created += len(batch)
            if self.progress is not None:
                self.progress(f"Embedding values: {reused} reused, {created} new")
        return Embedded(tuple(stored[text_hash] for text_hash in order), reused, created)
