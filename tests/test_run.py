"""Slim TOML load, apply, and quail run wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mcp.types import CallToolResult

from quail.cli import main as cli_main
from quail.config import ConfigError, load_config, parse_config
from quail.datasets import list_datasets, open_core_db
from quail.mcp import create_mcp_server
from quail.run import apply_config


def _as_dict(result: object) -> dict:
    if isinstance(result, CallToolResult):
        assert result.structuredContent is not None
        return dict(result.structuredContent)
    if isinstance(result, dict):
        return result
    raise AssertionError(f"Unexpected tool result type: {type(result)!r}")


def _write_manifest(tmp_path: Path, *, extra: str = "", auth_mode: str = "unrestricted") -> Path:
    csv_path = tmp_path / "data" / "notes.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "id,title,body\ne1,Hello,hydrangea\ne2,Other,climate\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        f"""
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"

[auth]
mode = "{auth_mode}"
workspace = "local"

[hosting]
bind = "127.0.0.1"
port = 8765

[[datasets]]
id = "notes"
source = "data/notes.csv"
name = "Notes"
{extra}
""",
        encoding="utf-8",
    )
    return manifest


def test_load_config_rejects_relative_path(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(ConfigError, match="absolute"):
        load_config("quail.toml")


def test_parse_rejects_unknown_keys(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, extra='\n[server]\nstate = "enabled"\n')
    with pytest.raises(ConfigError, match="Unknown root key"):
        load_config(manifest)


def test_parse_rejects_unknown_auth_mode(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, auth_mode="oidc")
    with pytest.raises(ConfigError, match="unrestricted|clerk"):
        load_config(manifest)


def test_paths_resolve_from_manifest_dir(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    config = load_config(manifest)
    assert config.database == (tmp_path / "data" / "quail.turso").resolve()
    assert config.feedback == (tmp_path / "data" / "feedback.jsonl").resolve()
    assert config.datasets[0].source == (tmp_path / "data" / "notes.csv").resolve()
    assert config.workspace_id == "local"
    assert config.bind == "127.0.0.1"
    assert config.port == 8765


def test_apply_imports_idempotent(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    config = load_config(manifest)
    db = apply_config(config)
    try:
        refs = list_datasets(db, "local")
        assert len(refs) == 1
        assert refs[0].dataset_id == "notes"
        assert refs[0].name == "Notes"
        assert refs[0].active_version_id
        version = refs[0].active_version_id
    finally:
        db.close()

    db2 = apply_config(config)
    try:
        refs2 = list_datasets(db2, "local")
        assert len(refs2) == 1
        assert refs2[0].active_version_id == version
    finally:
        db2.close()


def test_apply_missing_source_fails(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    (tmp_path / "data" / "notes.csv").unlink()
    config = load_config(manifest)
    with pytest.raises(ConfigError, match="source not found"):
        apply_config(config)
    assert not (tmp_path / "data" / "quail.turso").exists()


def test_apply_then_mcp_list_datasets(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    config = load_config(manifest)
    db = apply_config(config)
    db.close()
    assert config.workspace_id is not None
    server = create_mcp_server(
        config.database,
        config.feedback,
        workspace_id=config.workspace_id,
        host=config.bind,
        port=config.port,
    )

    async def run() -> None:
        result = _as_dict(await server.call_tool("quail_list_datasets", {}))
        assert result["datasets"][0]["dataset_id"] == "notes"

    asyncio.run(run())


def test_cli_run_requires_absolute_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del tmp_path
    with pytest.raises(SystemExit) as exited:
        cli_main(["run", "--config", "quail.toml"])
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert "absolute" in err


def test_parse_config_allows_empty_datasets(tmp_path: Path) -> None:
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        """
[core]
database = "core.turso"
feedback = "feedback.jsonl"

[auth]
mode = "unrestricted"
workspace = "local"

[hosting]
bind = "127.0.0.1"
port = 8000
""",
        encoding="utf-8",
    )
    config = parse_config(manifest.read_text(encoding="utf-8"), manifest_path=manifest.resolve())
    assert config.datasets == ()
    with open_core_db(config.database) as db:
        assert list_datasets(db, "local") == []
