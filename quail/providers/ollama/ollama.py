"""Ollama embedding HTTP client."""

from __future__ import annotations

from typing import Any, Sequence

from quail.providers.errors import ProviderError
from quail.providers.wire import post_json, require_vector


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
        body = post_json(
            url,
            payload,
            headers={},
            label="Ollama",
            config_key="ollama",
            http_hint="Confirm [providers.ollama].base_url and that the model is pulled.",
        )
        vectors = body.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ProviderError(
                "Ollama embedding response did not match the request size",
                repair_hint="Check the Ollama model and dimensions in the dataset embedding profile.",
            )
        return [require_vector(item, self._dimensions, label="Ollama") for item in vectors]
