"""Allowlisted evaluation archive builder tests."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

from scripts.build_eval_archives.build_eval_archives import build_archives


def test_builder_includes_only_runtime_files_and_can_split_search(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    for name in ("SETUP.md", "start.sh", "assemble.sh", "quail.toml"):
        (repo / name).write_text(name, encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "articles.csv").write_text("id,body\ne1,hello\n", encoding="utf-8")
    (repo / "data" / "quail.turso").write_bytes(b"core")
    (repo / "data" / "quail-search.turso.part00").write_bytes(b"search")
    (repo / "data" / "quail-search.turso.sha256").write_text("checksum\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "api.md").write_text("do not package", encoding="utf-8")
    (repo / "pyproject.toml").write_text("do not package", encoding="utf-8")
    (repo / "quail").mkdir()
    (repo / "quail" / "source.py").write_text("do not package", encoding="utf-8")
    subprocess.run(["git", "add", "data"], cwd=repo, check=True)

    wheel = tmp_path / "quail-0.11.0a0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    deps = tmp_path / "deps"
    deps.mkdir()
    (deps / "dependency.whl").write_bytes(b"dependency")
    (deps / ".DS_Store").write_bytes(b"metadata")
    output = tmp_path / "domain-base-quail.zip"
    search_output = tmp_path / "domain-search-parts.zip"

    build_archives(
        repo=repo,
        wheel=wheel,
        deps=deps,
        output=output,
        search_output=search_output,
    )

    root = "domain-base-quail/"
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
    assert names == {
        f"{root}SETUP.md",
        f"{root}start.sh",
        f"{root}assemble.sh",
        f"{root}quail.toml",
        f"{root}{wheel.name}",
        f"{root}deps/dependency.whl",
        f"{root}data/articles.csv",
        f"{root}data/quail.turso",
    }
    with zipfile.ZipFile(search_output) as archive:
        search_names = set(archive.namelist())
    assert search_names == {
        f"{root}SEARCH_PARTS.txt",
        f"{root}data/quail-search.turso.part00",
        f"{root}data/quail-search.turso.sha256",
    }
