"""Ollama embedding HTTP client."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Any, Sequence

from quail.providers.errors import ProviderError


class OllamaEmbedder:
    """POST /api/embed against a local or remote Ollama base URL."""

    def __init__(self, *, base_url: str, model: str, dimensions: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimensions = dimensions

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        url = f"{self._base_url}/api/embed"
        payload: dict[str, Any] = {
            "model": self._model,
            "input": list(texts),
            "dimensions": self._dimensions,
        }
        body = _post_json(url, payload, headers={})
        vectors = body.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ProviderError(
                "Ollama embedding response did not match the request size",
                repair_hint="Check the Ollama model and dimensions in the dataset embedding profile.",
            )
        return [_require_vector(item, self._dimensions, label="Ollama") for item in vectors]


def _post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise ProviderError(
            f"Ollama embedding HTTP {error.code}: {detail or error.reason}",
            repair_hint="Confirm [providers.ollama].base_url and that the model is pulled.",
        ) from error
    except urllib.error.URLError as error:
        raise ProviderError(
            f"Ollama embedding request failed: {error.reason}",
            repair_hint="Confirm [providers.ollama].base_url is reachable, then retry the exec.",
        ) from error
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError(
            "Ollama embedding response was not JSON",
            repair_hint="Confirm the Ollama base_url points at an Ollama server.",
        ) from error
    if not isinstance(body, dict):
        raise ProviderError(
            "Ollama embedding response was not a JSON object",
            repair_hint="Confirm the Ollama base_url points at an Ollama server.",
        )
    return body


def _require_vector(item: object, dimensions: int, *, label: str) -> list[float]:
    if not isinstance(item, list) or not item:
        raise ProviderError(
            f"{label} returned a non-vector embedding",
            repair_hint="Check the embedding model and dimensions configuration.",
        )
    if len(item) != dimensions:
        raise ProviderError(
            f"{label} returned {len(item)} dimensions; expected {dimensions}",
            repair_hint="Align datasets.embedding.dimensions with the provider output, then re-apply.",
        )
    vector: list[float] = []
    for value in item:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ProviderError(
                f"{label} returned a non-numeric embedding component",
                repair_hint="Check the embedding model response.",
            )
        number = float(value)
        if not math.isfinite(number):
            raise ProviderError(
                f"{label} returned a non-finite embedding component",
                repair_hint="Check the embedding model response.",
            )
        vector.append(number)
    return vector
