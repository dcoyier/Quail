"""Clerk auth, TOML allowlist, and sticky workspace tools."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp.types import CallToolResult

from quail.auth import ForbiddenError, StaticTokenVerifier, UnauthorizedError
from quail.auth.clerk import authenticate_bearer, resolve_allowlisted_user
from quail.config import ConfigError, UserSpec, load_config, parse_config
from quail.mcp import bearer_token, create_mcp_server_from_config
from quail.run import apply_config


def _as_dict(result: object) -> dict:
    if isinstance(result, CallToolResult):
        assert result.structuredContent is not None
        return dict(result.structuredContent)
    if isinstance(result, dict):
        return result
    raise AssertionError(f"Unexpected tool result type: {type(result)!r}")


def _is_error(result: object) -> bool:
    return isinstance(result, CallToolResult) and bool(result.isError)


def _write_clerk_manifest(
    tmp_path: Path,
    *,
    alice_default: str | None = "acme",
    alice_lock: bool = False,
) -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "notes.csv").write_text(
        "id,title\ne1,Hello\n",
        encoding="utf-8",
    )
    (data / "other.csv").write_text(
        "id,title\ne1,Labs\n",
        encoding="utf-8",
    )
    default_line = f'default_workspace = "{alice_default}"\n' if alice_default is not None else ""
    lock_line = "lock_workspace = true\n" if alice_lock else ""
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        f"""
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"

[auth]
mode = "clerk"
clerk_domain = "example.clerk.accounts.dev"

[hosting]
bind = "127.0.0.1"
port = 8765

[[workspaces]]
id = "acme"

[[workspaces.datasets]]
id = "notes"
source = "data/notes.csv"

[[workspaces]]
id = "labs"

[[workspaces.datasets]]
id = "other"
source = "data/other.csv"

[[users]]
id = "alice"
clerk_user_id = "user_alice"
workspaces = ["acme", "labs"]
{default_line}{lock_line}
""",
        encoding="utf-8",
    )
    return manifest


def test_clerk_parse_rejects_auth_workspace(tmp_path: Path) -> None:
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        """
[core]
database = "a.turso"
feedback = "f.jsonl"
[auth]
mode = "clerk"
clerk_domain = "example.clerk.accounts.dev"
workspace = "local"
[hosting]
bind = "127.0.0.1"
port = 8000
[[workspaces]]
id = "acme"
[[users]]
id = "alice"
clerk_user_id = "user_alice"
workspaces = ["acme"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="auth.workspace|Unknown auth key"):
        load_config(manifest)


def test_clerk_parse_rejects_root_datasets(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path)
    text = (
        manifest.read_text(encoding="utf-8")
        + '\n[[datasets]]\nid = "x"\nsource = "data/notes.csv"\n'
    )
    manifest.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="root \\[\\[datasets\\]\\]|Unknown root key"):
        load_config(manifest)


def test_clerk_lock_requires_default(tmp_path: Path) -> None:
    manifest = tmp_path / "bad.toml"
    manifest.write_text(
        """
[core]
database = "a.turso"
feedback = "f.jsonl"
[auth]
mode = "clerk"
clerk_domain = "example.clerk.accounts.dev"
[hosting]
bind = "127.0.0.1"
port = 8000
[[workspaces]]
id = "acme"
[[users]]
id = "alice"
clerk_user_id = "user_alice"
workspaces = ["acme"]
lock_workspace = true
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="lock_workspace requires default_workspace"):
        load_config(manifest)


def test_authenticate_bearer_allowlist() -> None:
    users = (
        UserSpec(
            user_id="alice",
            clerk_user_id="user_alice",
            workspaces=("acme",),
            default_workspace="acme",
            lock_workspace=False,
        ),
    )
    verifier = StaticTokenVerifier({"tok": "user_alice"})
    principal = authenticate_bearer("Bearer tok", verifier=verifier, users=users)
    assert principal.user.user_id == "alice"
    with pytest.raises(UnauthorizedError):
        authenticate_bearer(None, verifier=verifier, users=users)
    with pytest.raises(ForbiddenError):
        authenticate_bearer(
            "Bearer tok",
            verifier=StaticTokenVerifier({"tok": "user_other"}),
            users=users,
        )
    assert resolve_allowlisted_user(users, "user_alice").user_id == "alice"


def test_clerk_unbound_requires_switch(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path, alice_default=None)
    config = load_config(manifest)
    apply_config(config).close()
    verifier = StaticTokenVerifier({"alice-token": "user_alice"})
    server = create_mcp_server_from_config(config, verifier=verifier)

    async def run() -> None:
        with bearer_token("alice-token"):
            listed = _as_dict(await server.call_tool("quail_list_workspaces", {}))
            assert listed["active_workspace_id"] is None
            assert {w["workspace_id"] for w in listed["workspaces"]} == {"acme", "labs"}
            failed = await server.call_tool("quail_list_datasets", {})
            assert _is_error(failed)
            assert "quail_switch_workspace" in (
                _as_dict(failed)["diagnostic"].get("repair_hint") or ""
            )
            switched = _as_dict(
                await server.call_tool("quail_switch_workspace", {"workspace_id": "labs"})
            )
            assert switched["active_workspace_id"] == "labs"
            datasets = _as_dict(await server.call_tool("quail_list_datasets", {}))
            assert datasets["datasets"][0]["dataset_id"] == "other"

    asyncio.run(run())


def test_clerk_default_binds_and_switch_allowed(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path, alice_default="acme", alice_lock=False)
    config = load_config(manifest)
    apply_config(config).close()
    verifier = StaticTokenVerifier({"alice-token": "user_alice"})
    server = create_mcp_server_from_config(config, verifier=verifier)

    async def run() -> None:
        with bearer_token("alice-token"):
            listed = _as_dict(await server.call_tool("quail_list_workspaces", {}))
            assert listed["active_workspace_id"] == "acme"
            datasets = _as_dict(await server.call_tool("quail_list_datasets", {}))
            assert datasets["datasets"][0]["dataset_id"] == "notes"
            switched = _as_dict(
                await server.call_tool("quail_switch_workspace", {"workspace_id": "labs"})
            )
            assert switched["active_workspace_id"] == "labs"

    asyncio.run(run())


def test_clerk_lock_blocks_switch(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path, alice_default="acme", alice_lock=True)
    config = load_config(manifest)
    apply_config(config).close()
    verifier = StaticTokenVerifier({"alice-token": "user_alice"})
    server = create_mcp_server_from_config(config, verifier=verifier)

    async def run() -> None:
        with bearer_token("alice-token"):
            listed = _as_dict(await server.call_tool("quail_list_workspaces", {}))
            assert listed["workspaces"] == [{"workspace_id": "acme"}]
            assert listed["active_workspace_id"] == "acme"
            failed = await server.call_tool("quail_switch_workspace", {"workspace_id": "labs"})
            assert _is_error(failed)
            assert "locked" in (_as_dict(failed)["diagnostic"].get("repair_hint") or "").lower()

    asyncio.run(run())


def test_clerk_session_workspace_mismatch_after_switch(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path, alice_default="acme")
    config = load_config(manifest)
    apply_config(config).close()
    verifier = StaticTokenVerifier({"alice-token": "user_alice"})
    server = create_mcp_server_from_config(config, verifier=verifier)

    async def run() -> None:
        with bearer_token("alice-token"):
            started = _as_dict(await server.call_tool("quail_start_session", {}))
            session_id = started["session_id"]
            assert started["workspace_id"] == "acme"
            await server.call_tool("quail_switch_workspace", {"workspace_id": "labs"})
            failed = await server.call_tool(
                "quail_exec",
                {
                    "session_id": session_id,
                    "dataset_id": "other",
                    "code": "print(1)\n",
                },
            )
            assert _is_error(failed)

    asyncio.run(run())


def test_clerk_missing_token(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path)
    config = load_config(manifest)
    apply_config(config).close()
    server = create_mcp_server_from_config(
        config, verifier=StaticTokenVerifier({"alice-token": "user_alice"})
    )

    async def run() -> None:
        failed = await server.call_tool("quail_list_workspaces", {})
        assert _is_error(failed)

    asyncio.run(run())


def test_parse_clerk_example_shape(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path)
    config = parse_config(manifest.read_text(encoding="utf-8"), manifest_path=manifest.resolve())
    assert config.auth_mode == "clerk"
    assert config.clerk_domain == "example.clerk.accounts.dev"
    assert config.workspace_id is None
    assert len(config.workspaces) == 2
    assert len(config.users) == 1
    assert config.users[0].default_workspace == "acme"
