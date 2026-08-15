"""Resolve and read the model-facing analysis contract (`docs/api.md`)."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_API_DOCS = _REPO_ROOT / "docs" / "api.md"
_PACKAGED_PARTS = ("data", "api.md")
_MAINTAINER_MARK = "\n## Notes for maintainers"


def _agent_facing_docs(text: str) -> str:
    """Drop maintainer notes so agents do not treat operator knobs as contract."""

    if text.startswith("## Notes for maintainers"):
        return ""
    index = text.find(_MAINTAINER_MARK)
    if index == -1:
        return text if text.endswith("\n") else f"{text}\n"
    return text[:index].rstrip() + "\n"


def load_api_docs(override: str | Path | None = None) -> str:
    """Give back analysis docs text from an override path or the packaged default."""

    if override is not None:
        raw = Path(override).expanduser().resolve().read_text(encoding="utf-8")
        return _agent_facing_docs(raw)
    packaged = resources.files("quail").joinpath(*_PACKAGED_PARTS)
    if packaged.is_file():
        return _agent_facing_docs(packaged.read_text(encoding="utf-8"))
    if _REPO_API_DOCS.is_file():
        return _agent_facing_docs(_REPO_API_DOCS.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        "Analysis docs not found: packaged quail/data/api.md missing and "
        f"{_REPO_API_DOCS} is not readable."
    )
