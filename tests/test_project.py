from pathlib import Path

import pytest

from quail.project import KernelLimits, Project, QuailError, load_manifest

STORAGE_EXAMPLE = """\
[project]
quail = "1"

[datasets.notes]
source = "data/notes.csv"
id = "id"
embed = "ollama/embeddinggemma:latest"

[providers.ollama]
base_url = "http://127.0.0.1:11434"

[providers.openai]
base_url = "https://api.openai.com/v1"
api_key = "env:OPENAI_API_KEY"

[kernel]
cpu_seconds = 30
wall_seconds = 120
memory_mb = 1024
max_limit = 1000
output_kib = 64
"""


def _toml(root: Path, text: str) -> Path:
    path = root / "quail.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_manifest_uses_kernel_defaults(tmp_path: Path) -> None:
    _toml(tmp_path, '[project]\nquail = "1"\n')
    project = Project(tmp_path)
    assert project.manifest.schema == "1"
    assert project.manifest.datasets == {}
    assert project.manifest.providers == {}
    assert project.manifest.kernel == KernelLimits()


def test_storage_example_resolves_paths(tmp_path: Path) -> None:
    _toml(tmp_path, STORAGE_EXAMPLE)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "notes.csv").write_text("id,body\n", encoding="utf-8")
    project = Project(tmp_path)
    notes = project.manifest.datasets["notes"]
    assert notes.source == (tmp_path / "data" / "notes.csv").resolve()
    assert notes.id_column == "id"
    assert notes.embed == "ollama/embeddinggemma:latest"
    assert project.dataset_source("notes") == notes.source
    assert project.index_path("notes") == tmp_path.resolve() / ".quail" / "notes.quail"
    assert project.session_log_dir("billing-coding") == (
        tmp_path.resolve() / "sessions" / "billing-coding" / "log"
    )
    openai = project.manifest.providers["openai"]
    assert openai.api_key_env == "OPENAI_API_KEY"
    assert openai.base_url == "https://api.openai.com/v1"


def test_project_accepts_toml_file_path(tmp_path: Path) -> None:
    path = _toml(tmp_path, '[project]\nquail = "1"\n')
    project = Project(path)
    assert project.root == tmp_path.resolve()
    assert project.manifest_path == path.resolve()


def test_missing_manifest_is_error(tmp_path: Path) -> None:
    with pytest.raises(QuailError, match="no quail.toml"):
        Project(tmp_path)


def test_unknown_root_key_is_error(tmp_path: Path) -> None:
    _toml(tmp_path, '[project]\nquail = "1"\n\n[hosting]\nurl = "x"\n')
    with pytest.raises(QuailError, match="unknown key 'hosting'"):
        Project(tmp_path)


def test_unknown_dataset_key_is_error(tmp_path: Path) -> None:
    _toml(
        tmp_path,
        '[project]\nquail = "1"\n\n[datasets.notes]\nsource = "data/notes.csv"\ntype = "csv"\n',
    )
    with pytest.raises(QuailError, match="unknown key 'type'"):
        Project(tmp_path)


def test_api_key_literal_is_error(tmp_path: Path) -> None:
    _toml(
        tmp_path,
        '[project]\nquail = "1"\n\n[providers.openai]\n'
        'base_url = "https://api.openai.com/v1"\napi_key = "sk-secret"\n',
    )
    with pytest.raises(QuailError, match="env:NAME"):
        Project(tmp_path)


def test_ollama_table_fills_default_url(tmp_path: Path) -> None:
    _toml(tmp_path, '[project]\nquail = "1"\n\n[providers.ollama]\n')
    project = Project(tmp_path)
    assert project.manifest.providers["ollama"].base_url == "http://127.0.0.1:11434"


def test_kernel_partial_table_keeps_other_defaults(tmp_path: Path) -> None:
    _toml(tmp_path, '[project]\nquail = "1"\n\n[kernel]\nmax_limit = 50\n')
    project = Project(tmp_path)
    assert project.manifest.kernel.max_limit == 50
    assert project.manifest.kernel.cpu_seconds == 30


def test_kernel_zero_is_error(tmp_path: Path) -> None:
    _toml(tmp_path, '[project]\nquail = "1"\n\n[kernel]\ncpu_seconds = 0\n')
    with pytest.raises(QuailError, match="positive"):
        Project(tmp_path)


def test_quail_version_must_be_string_one(tmp_path: Path) -> None:
    _toml(tmp_path, "[project]\nquail = 1\n")
    with pytest.raises(QuailError, match="must be a string"):
        Project(tmp_path)


def test_load_manifest_reads_the_file_directly(tmp_path: Path) -> None:
    path = _toml(tmp_path, '[project]\nquail = "1"\n')
    manifest = load_manifest(path)
    assert manifest.schema == "1"
