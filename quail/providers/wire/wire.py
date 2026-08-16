"""Shared HTTP plumbing for embedding providers."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Any

from quail.providers.errors import ProviderError


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    label: str,
    config_key: str,
    http_hint: str,
) -> dict[str, Any]:
    """POST JSON and give back the decoded JSON object body.

    Raise ProviderError on HTTP, transport, or decode failures. label names
    the provider in messages; config_key names its [providers.*] TOML table;
    http_hint repairs HTTP-status failures.
    """

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
            f"{label} embedding HTTP {error.code}: {detail or error.reason}",
            repair_hint=http_hint,
        ) from error
    except urllib.error.URLError as error:
        raise ProviderError(
            f"{label} embedding request failed: {error.reason}",
            repair_hint=f"Confirm [providers.{config_key}].base_url is reachable, then retry the exec.",
        ) from error
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError(
            f"{label} embedding response was not JSON",
            repair_hint=f"Confirm [providers.{config_key}].base_url.",
        ) from error
    if not isinstance(body, dict):
        raise ProviderError(
            f"{label} embedding response was not a JSON object",
            repair_hint=f"Confirm [providers.{config_key}].base_url.",
        )
    return body


def require_vector(item: object, dimensions: int, *, label: str) -> list[float]:
    """Require one finite float vector of exactly dimensions components."""

    if not isinstance(item, list) or not item:
        raise ProviderError(
            f"{label} returned a non-vector embedding",
            repair_hint="Check the embedding model and dimensions configuration.",
        )
    if len(item) != dimensions:
        raise ProviderError(
            f"{label} returned {len(item)} dimensions; expected {dimensions}",
            repair_hint="Align datasets.embedding.dimensions with the provider output, then re-run quail process.",
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
