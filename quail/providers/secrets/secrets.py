"""Resolve env:NAME secret references."""

from __future__ import annotations

import os

from quail.providers.errors import ProviderError


def resolve_env_ref(value: str, *, label: str) -> str:
    """Resolve env:NAME to the environment value; reject raw secrets."""

    if not value.startswith("env:"):
        raise ProviderError(
            f"{label} must be an env:NAME reference",
            repair_hint=f'Set {label} = "env:SOME_VAR" in quail.toml.',
        )
    name = value[4:].strip()
    if not name:
        raise ProviderError(
            f"{label} env reference is empty",
            repair_hint=f'Set {label} = "env:SOME_VAR" in quail.toml.',
        )
    resolved = os.environ.get(name)
    if resolved is None or resolved == "":
        raise ProviderError(
            f"Environment variable {name} is missing or empty",
            repair_hint=f"Export {name} and restart Quail.",
        )
    return resolved
