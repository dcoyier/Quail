"""OpenRouter embedding HTTP client."""

from __future__ import annotations

from typing import Any, Sequence

from quail.providers.errors import ProviderError
from quail.providers.wire import post_json, require_vector


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
        body = post_json(
            url,
            payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            label="OpenRouter",
            config_key="openrouter",
            http_hint="Confirm OpenRouter credentials and model id, then retry the exec.",
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
            ordered[index] = require_vector(
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
