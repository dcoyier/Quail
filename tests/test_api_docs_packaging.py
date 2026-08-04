"""RAG wheels do not expose the analysis-language API documentation."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

from quail.mcp.api_docs import load_api_docs

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_API_DOCS = _REPO_ROOT / "docs" / "api.md"


def test_load_api_docs_default_matches_repo() -> None:
    text = load_api_docs()
    assert "Quail Analysis API" in text
    assert text == _REPO_API_DOCS.read_text(encoding="utf-8")


def test_load_api_docs_override(tmp_path: Path) -> None:
    path = tmp_path / "custom.md"
    path.write_text("# custom docs\n", encoding="utf-8")
    assert load_api_docs(path) == "# custom docs\n"


def test_rag_wheel_excludes_packaged_api_docs(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        assert "quail/data/api.md" not in archive.namelist()
