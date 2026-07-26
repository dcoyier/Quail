"""Resolve and read the model-facing analysis contract (`docs/api.md`)."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_API_DOCS = _REPO_ROOT / "docs" / "api.md"
_PACKAGED_PARTS = ("data", "api.md")


def load_api_docs(override: str | Path | None = None) -> str:
    """Give back analysis docs text from an override path or the packaged default."""

    if override is not None:
        return Path(override).expanduser().resolve().read_text(encoding="utf-8")
    packaged = resources.files("quail").joinpath(*_PACKAGED_PARTS)
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    if _REPO_API_DOCS.is_file():
        return _REPO_API_DOCS.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "Analysis docs not found: packaged quail/data/api.md missing and "
        f"{_REPO_API_DOCS} is not readable."
    )
