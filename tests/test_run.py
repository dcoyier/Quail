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


def test_example_quail_toml_pins_distinct_search_database() -> None:
    manifest = Path(__file__).resolve().parents[1] / "examples" / "quail.toml"
    config = load_config(manifest)
    assert config.search_database is not None
    assert config.search_database != config.database
    assert config.search_database.name == "quail-search.turso"


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


def test_apply_fails_when_lease_held(tmp_path: Path) -> None:
    from quail.analysis.errors import QuailRuntimeError
    from quail.run.lease import acquire_deployment_lease

    manifest = _write_manifest(tmp_path)
    config = load_config(manifest)
    with acquire_deployment_lease(config):
        with pytest.raises(QuailRuntimeError, match="deployment lease"):
            apply_config(config)


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


def test_cli_run_help_matches_serve_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        cli_main(["--help"])
    assert exited.value.code == 0
    parent = capsys.readouterr().out
    assert "Apply slim quail.toml then serve unrestricted loopback MCP" not in parent
    assert "Serve MCP if already processed (never activates)" in parent

    with pytest.raises(SystemExit) as exited:
        cli_main(["run", "--help"])
    assert exited.value.code == 0
    run_help = capsys.readouterr().out
    assert "never activates" in run_help.lower() or "already processed" in run_help.lower()
    assert "unrestricted loopback" not in run_help.lower()
    assert "apply slim" not in run_help.lower()


def test_cli_process_help_matches_process_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        cli_main(["--help"])
    assert exited.value.code == 0
    parent = capsys.readouterr().out
    assert "Apply slim quail.toml then warm" not in parent
    assert "Import, warm search, then publish activation" in parent

    with pytest.raises(SystemExit) as exited:
        cli_main(["process", "--help"])
    assert exited.value.code == 0
    process_help = capsys.readouterr().out
    assert "apply slim" not in process_help.lower()
    assert "lease" in process_help.lower()
    assert "publish" in process_help.lower()
    assert "never writes" in process_help.lower()


def test_cli_run_requires_absolute_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del tmp_path
    with pytest.raises(SystemExit) as exited:
        cli_main(["run", "--config", "quail.toml"])
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert "absolute" in err


def test_cli_process_prints_repair_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from quail.analysis.errors import QuailRuntimeError

    manifest = _write_unrestricted_hosting(tmp_path)

    def boom(_config: object, clear: bool = False) -> None:
        del clear
        raise QuailRuntimeError(
            "Another Quail process holds the deployment lease (x.quail.lock).",
            repair_hint="Stop the running Quail server, then retry.",
        )

    monkeypatch.setattr("quail.cli.cli.process_config", boom)
    with pytest.raises(SystemExit) as exited:
        cli_main(["process", "--config", str(manifest.resolve())])
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert "deployment lease" in err
    assert "Stop the running Quail server" in err


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


def _write_unrestricted_hosting(
    tmp_path: Path,
    *,
    bind: str = "127.0.0.1",
    hosting_extra: str = "",
) -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "notes.csv").write_text("id,title\ne1,Hello\n", encoding="utf-8")
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        f"""
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"

[auth]
mode = "unrestricted"
workspace = "local"

[hosting]
bind = "{bind}"
port = 8765
{hosting_extra}
[[datasets]]
id = "notes"
source = "data/notes.csv"
""",
        encoding="utf-8",
    )
    return manifest


def test_unrestricted_rejects_non_loopback_bind(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="allow_public_unrestricted"):
        load_config(_write_unrestricted_hosting(tmp_path, bind="1.2.3.4"))


def test_unrestricted_rejects_wildcard_bind_without_public_base_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="public_base_url is required"):
        load_config(_write_unrestricted_hosting(tmp_path, bind="0.0.0.0"))


def test_unrestricted_rejects_public_base_url_even_on_loopback_bind(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="public_base_url|allow_public_unrestricted"):
        load_config(
            _write_unrestricted_hosting(
                tmp_path,
                bind="127.0.0.1",
                hosting_extra='public_base_url = "https://example.trycloudflare.com"\n',
            )
        )


def test_unrestricted_allows_public_with_override(tmp_path: Path) -> None:
    config = load_config(
        _write_unrestricted_hosting(
            tmp_path,
            bind="0.0.0.0",
            hosting_extra=(
                'public_base_url = "https://example.trycloudflare.com"\n'
                "allow_public_unrestricted = true\n"
            ),
        )
    )
    assert config.allow_public_unrestricted is True
    assert config.bind == "0.0.0.0"
    assert config.public_base_url == "https://example.trycloudflare.com"


def test_unrestricted_wildcard_bind_requires_explicit_public_base_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="public_base_url is required"):
        load_config(
            _write_unrestricted_hosting(
                tmp_path,
                bind="0.0.0.0",
                hosting_extra="allow_public_unrestricted = true\n",
            )
        )


def test_clerk_rejects_allow_public_unrestricted(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "notes.csv").write_text("id,title\ne1,Hello\n", encoding="utf-8")
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        """
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"

[auth]
mode = "clerk"
clerk_domain = "example.clerk.accounts.dev"

[hosting]
bind = "127.0.0.1"
port = 8765
allow_public_unrestricted = true

[[workspaces]]
id = "acme"

[[workspaces.datasets]]
id = "notes"
source = "data/notes.csv"

[[users]]
id = "alice"
clerk_user_id = "user_alice"
workspaces = ["acme"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="allow_public_unrestricted"):
        load_config(manifest)
