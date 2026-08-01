"""OpenRouter embedding HTTP client."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Any, Sequence

from quail.providers.errors import ProviderError


class OpenRouterEmbedder:
    """POST /embeddings against OpenRouter-compatible APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        url = f"{self._base_url}/embeddings"
        payload: dict[str, Any] = {
            "model": self._model,
            "input": list(texts),
            "dimensions": self._dimensions,
        }
        body = _post_json(
            url,
            payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ProviderError(
                "OpenRouter embedding response did not match the request size",
                repair_hint="Check the OpenRouter model and dimensions in the dataset embedding profile.",
            )
        # Preserve request order via index when present.
        ordered: list[list[float] | None] = [None] * len(texts)
        for item in data:
            if not isinstance(item, dict):
                raise ProviderError(
                    "OpenRouter embedding data entry was not an object",
                    repair_hint="Check the OpenRouter embedding response.",
                )
            index = item.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                index = data.index(item)
            if index < 0 or index >= len(texts):
                raise ProviderError(
                    "OpenRouter embedding index was out of range",
                    repair_hint="Check the OpenRouter embedding response.",
                )
            ordered[index] = _require_vector(
                item.get("embedding"),
                self._dimensions,
                label="OpenRouter",
            )
        if any(vector is None for vector in ordered):
            raise ProviderError(
                "OpenRouter embedding response was incomplete",
                repair_hint="Check the OpenRouter embedding response.",
            )
        return [vector for vector in ordered if vector is not None]


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
            f"OpenRouter embedding HTTP {error.code}: {detail or error.reason}",
            repair_hint="Confirm OpenRouter credentials and model id, then retry the exec.",
        ) from error
    except urllib.error.URLError as error:
        raise ProviderError(
            f"OpenRouter embedding request failed: {error.reason}",
            repair_hint="Confirm [providers.openrouter].base_url is reachable, then retry the exec.",
        ) from error
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError(
            "OpenRouter embedding response was not JSON",
            repair_hint="Confirm the OpenRouter base_url.",
        ) from error
    if not isinstance(body, dict):
        raise ProviderError(
            "OpenRouter embedding response was not a JSON object",
            repair_hint="Confirm the OpenRouter base_url.",
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
