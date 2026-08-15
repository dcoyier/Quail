"""Clerk auth, TOML allowlist, and sticky workspace tools."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp.types import CallToolResult

from quail.auth import AllowlistedPrincipal, ForbiddenError, StaticTokenVerifier, UnauthorizedError
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
clerk_authorized_parties = ["test_clerk_app"]

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
clerk_authorized_parties = ["test_clerk_app"]
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
clerk_authorized_parties = ["test_clerk_app"]
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
    server = create_mcp_server_from_config(config, verifier=verifier).server

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
    server = create_mcp_server_from_config(config, verifier=verifier).server

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
    server = create_mcp_server_from_config(config, verifier=verifier).server

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
    server = create_mcp_server_from_config(config, verifier=verifier).server

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
            exported = await server.call_tool(
                "quail_export_csv",
                {"session_id": session_id, "dataset_id": "other"},
            )
            assert _is_error(exported)
            assert _as_dict(exported)["diagnostic"]["stable_error_code"] == "quail_syntax_error"

    asyncio.run(run())


def test_clerk_missing_token(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path)
    config = load_config(manifest)
    apply_config(config).close()
    server = create_mcp_server_from_config(
        config, verifier=StaticTokenVerifier({"alice-token": "user_alice"})
    ).server

    async def run() -> None:
        failed = await server.call_tool("quail_list_workspaces", {})
        assert _is_error(failed)

    asyncio.run(run())


def test_parse_clerk_example_shape(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path)
    config = parse_config(manifest.read_text(encoding="utf-8"), manifest_path=manifest.resolve())
    assert config.auth_mode == "clerk"
    assert config.clerk_domain == "example.clerk.accounts.dev"
    assert config.clerk_authorized_parties == ("test_clerk_app",)
    assert config.workspace_id is None
    assert len(config.workspaces) == 2
    assert len(config.users) == 1
    assert config.users[0].default_workspace == "acme"


def test_clerk_parse_requires_authorized_parties(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path)
    text = manifest.read_text(encoding="utf-8").replace(
        'clerk_authorized_parties = ["test_clerk_app"]\n',
        "",
    )
    manifest.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="clerk_authorized_parties"):
        load_config(manifest)


def test_authorized_party_claim_matching() -> None:
    from quail.auth.clerk.clerk import _require_authorized_party

    parties = frozenset({"app_a", "app_b"})
    _require_authorized_party({"azp": "app_a", "sub": "u"}, parties)
    _require_authorized_party({"aud": "app_b", "sub": "u"}, parties)
    _require_authorized_party({"aud": ["other", "app_a"], "sub": "u"}, parties)
    _require_authorized_party({"client_id": "app_b", "sub": "u"}, parties)
    with pytest.raises(UnauthorizedError, match="authorized party"):
        _require_authorized_party({"sub": "u"}, parties)
    with pytest.raises(UnauthorizedError, match="not for this Quail"):
        _require_authorized_party({"azp": "app_other", "sub": "u"}, parties)


def test_clerk_session_owner_blocks_other_user(tmp_path: Path) -> None:
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
clerk_authorized_parties = ["test_clerk_app"]

[hosting]
bind = "127.0.0.1"
port = 8765

[[workspaces]]
id = "acme"

[[workspaces.datasets]]
id = "notes"
source = "data/notes.csv"

[[users]]
id = "alice"
clerk_user_id = "user_alice"
workspaces = ["acme"]
default_workspace = "acme"

[[users]]
id = "bob"
clerk_user_id = "user_bob"
workspaces = ["acme"]
default_workspace = "acme"
""",
        encoding="utf-8",
    )
    config = load_config(manifest)
    apply_config(config).close()
    server = create_mcp_server_from_config(
        config,
        verifier=StaticTokenVerifier({"alice-token": "user_alice", "bob-token": "user_bob"}),
    ).server

    async def run() -> None:
        with bearer_token("alice-token"):
            started = _as_dict(await server.call_tool("quail_start_session", {}))
            session_id = started["session_id"]
            ok = await server.call_tool(
                "quail_exec",
                {
                    "session_id": session_id,
                    "dataset_id": "notes",
                    "code": "print(1)",
                },
            )
            assert not _is_error(ok)
        with bearer_token("bob-token"):
            stolen = await server.call_tool(
                "quail_exec",
                {
                    "session_id": session_id,
                    "dataset_id": "notes",
                    "code": "print(2)",
                },
            )
            assert _is_error(stolen)
            stolen_export = await server.call_tool(
                "quail_export_csv",
                {"session_id": session_id, "dataset_id": "notes"},
            )
            assert _is_error(stolen_export)
            assert (
                _as_dict(stolen_export)["diagnostic"]["stable_error_code"]
                == "quail_scope_error"
            )

    asyncio.run(run())


def test_clerk_export_csv_writes_host_file(tmp_path: Path) -> None:
    manifest = _write_clerk_manifest(tmp_path)
    config = load_config(manifest)
    apply_config(config).close()
    server = create_mcp_server_from_config(
        config, verifier=StaticTokenVerifier({"alice-token": "user_alice"})
    ).server

    async def run() -> None:
        with bearer_token("alice-token"):
            setup = _as_dict(await server.call_tool("quail_setup", {}))
            session_id = setup["session_id"]
            tagged = _as_dict(
                await server.call_tool(
                    "quail_exec",
                    {
                        "session_id": session_id,
                        "dataset_id": "notes",
                        "code": (
                            'topic = create_field("topic")\n'
                            'tag(G0, topic, "climate")\n'
                            'print("ok")\n'
                        ),
                    },
                )
            )
            assert "ok" in tagged["printed_output"]
            exported = _as_dict(
                await server.call_tool(
                    "quail_export_csv",
                    {"session_id": session_id, "dataset_id": "notes"},
                )
            )
            path = Path(exported["path"])
            assert path.is_file()
            assert path.parent == config.database.resolve().parent / "exports"
            assert exported["columns"] == ["id", "title", "topic"]
            assert exported["row_count"] == 1
            lines = path.read_text(encoding="utf-8").splitlines()
            assert lines[0] == "id,title,topic"
            assert all(line.endswith(",climate") for line in lines[1:])

    asyncio.run(run())


def test_mcp_session_id_reads_streamable_http_header() -> None:
    from quail.mcp.server.server import _clerk_connection_key, _mcp_session_id

    class _Request:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

    class _RequestContext:
        def __init__(self, headers: dict[str, str]) -> None:
            self.request = _Request(headers)

    class _Ctx:
        def __init__(self, headers: dict[str, str]) -> None:
            self.request_context = _RequestContext(headers)

        @property
        def session(self) -> object:
            raise AttributeError("ServerSession has no id")

    assert _mcp_session_id(_Ctx({"mcp-session-id": "conn-1"})) == "conn-1"
    assert _mcp_session_id(_Ctx({})) is None
    principal = AllowlistedPrincipal(
        user=UserSpec(
            user_id="alice",
            clerk_user_id="user_alice",
            workspaces=("acme",),
            default_workspace="acme",
            lock_workspace=False,
        ),
        clerk_user_id="user_alice",
    )
    assert _clerk_connection_key(principal, _Ctx({"mcp-session-id": "conn-1"})) == "sess:conn-1"
    assert _clerk_connection_key(principal, _Ctx({})) == "user:user_alice"


def test_sticky_workspace_rejects_non_member_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quail.mcp.sticky import StickyWorkspaceStore

    manifest = _write_clerk_manifest(tmp_path)
    config = load_config(manifest)
    apply_config(config).close()
    verifier = StaticTokenVerifier({"alice-token": "user_alice"})
    server = create_mcp_server_from_config(config, verifier=verifier).server

    def _poisoned(self: StickyWorkspaceStore, connection_key: str, user: UserSpec) -> str | None:
        del self, connection_key, user
        return "not-a-workspace"

    monkeypatch.setattr(StickyWorkspaceStore, "ensure_initial_bind", _poisoned)

    async def run() -> None:
        with bearer_token("alice-token"):
            failed = await server.call_tool("quail_list_datasets", {})
            assert _is_error(failed)

    asyncio.run(run())


def test_clerk_connector_file_route_requires_bearer(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    from quail.connectors.load import ConnectedProvider, ConnectorCatalog, WorkspaceConnectorBundle
    from quail.connectors.sdk import ConnectorError, ConnectorManifest, FileResponse, RouteSpec
    from quail.mcp.server import create_clerk_mcp_server

    asset = tmp_path / "page.png"
    asset.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 20)

    class _RouteProvider:
        def call_tool(self, context, name, arguments):
            raise ConnectorError("UNKNOWN_TOOL", "none", "n/a")

        def dataset_document(self, context, dataset_id):
            return None

        def handle_route(self, context, route_id, path_params):
            if route_id != "asset" or path_params.get("token") != "good":
                return None
            if context.workspace_id != "acme" or context.user_id != "alice":
                return None
            return FileResponse(asset, "image/png", filename="page.png")

    class _RouteConnector:
        def __init__(self) -> None:
            self._manifest = ConnectorManifest(
                id="garden_gate",
                version="1.0.0",
                routes=(RouteSpec(id="asset", path_suffix="assets/{token}"),),
            )

        @property
        def manifest(self):
            return self._manifest

        def read_resource(self, uri: str) -> str:
            raise ValueError(uri)

        def connect(self, runtime):
            del runtime
            return _RouteProvider()

    connector = _RouteConnector()
    connected = ConnectedProvider(
        extension_id="garden_gate",
        version="1.0.0",
        manifest=connector.manifest,
        connector=connector,
        provider=_RouteProvider(),
        dataset_ids=frozenset(),
        provides_docs=False,
    )
    catalog = ConnectorCatalog(
        by_workspace={
            "acme": WorkspaceConnectorBundle(
                workspace_id="acme",
                providers=(connected,),
                docs_by_dataset={},
            )
        }
    )
    manifest = _write_clerk_manifest(tmp_path)
    config = load_config(manifest)
    apply_config(config).close()
    verifier = StaticTokenVerifier({"alice-token": "user_alice", "bob-token": "user_bob"})
    server = create_clerk_mcp_server(config, verifier=verifier, connector_catalog=catalog)
    client = TestClient(server.streamable_http_app())
    url = "/extensions/garden_gate/acme/assets/good"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"Authorization": "Bearer bob-token"}).status_code == 403
    labs = client.get(
        "/extensions/garden_gate/labs/assets/good",
        headers={"Authorization": "Bearer alice-token"},
    )
    assert labs.status_code == 404
    ok = client.get(url, headers={"Authorization": "Bearer alice-token"})
    assert ok.status_code == 200
    assert ok.content.startswith(b"\x89PNG")


def _acme_ping_catalog() -> object:
    from quail.connectors.load import ConnectedProvider, ConnectorCatalog, WorkspaceConnectorBundle
    from quail.connectors.sdk import ConnectorManifest, ToolResult, ToolSpec

    class _PingProvider:
        def call_tool(self, context, name, arguments):
            del context, name, arguments
            return ToolResult(structured_content={"ok": True})

        def dataset_document(self, context, dataset_id):
            del context, dataset_id
            return None

        def handle_route(self, context, route_id, path_params):
            del context, route_id, path_params
            return None

    class _PingConnector:
        def __init__(self) -> None:
            self._manifest = ConnectorManifest(
                id="garden_gate",
                version="1.0.0",
                tools=(
                    ToolSpec(
                        name="garden_ping",
                        title="Ping",
                        description="Workspace-bound ping.",
                        input_schema={
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    ),
                ),
            )

        @property
        def manifest(self):
            return self._manifest

        def read_resource(self, uri: str) -> str:
            raise ValueError(uri)

        def connect(self, runtime):
            del runtime
            return _PingProvider()

    connector = _PingConnector()
    connected = ConnectedProvider(
        extension_id="garden_gate",
        version="1.0.0",
        manifest=connector.manifest,
        connector=connector,
        provider=_PingProvider(),
        dataset_ids=frozenset(),
        provides_docs=False,
    )
    return ConnectorCatalog(
        by_workspace={
            "acme": WorkspaceConnectorBundle(
                workspace_id="acme",
                providers=(connected,),
                docs_by_dataset={},
            )
        }
    )


def test_clerk_tools_list_follows_sticky_workspace(tmp_path: Path) -> None:
    from quail.mcp.server import create_clerk_mcp_server

    catalog = _acme_ping_catalog()
    manifest = _write_clerk_manifest(tmp_path)
    config = load_config(manifest)
    apply_config(config).close()
    verifier = StaticTokenVerifier({"alice-token": "user_alice"})
    server = create_clerk_mcp_server(config, verifier=verifier, connector_catalog=catalog)

    async def run() -> None:
        with bearer_token("alice-token"):
            names = {tool.name for tool in await server.list_tools()}
            assert "garden_ping" in names
            assert "quail_exec" in names
            assert "quail_export_csv" in names
            assert "quail_switch_workspace" in names
            ping = _as_dict(await server.call_tool("garden_ping", {}))
            assert ping["ok"] is True
            switched = _as_dict(
                await server.call_tool("quail_switch_workspace", {"workspace_id": "labs"})
            )
            assert switched["active_workspace_id"] == "labs"
            names = {tool.name for tool in await server.list_tools()}
            assert "garden_ping" not in names
            assert "quail_exec" in names
            assert "quail_export_csv" in names
            assert "quail_switch_workspace" in names
            failed = await server.call_tool("garden_ping", {})
            assert _is_error(failed)
            assert (
                _as_dict(failed)["diagnostic"]["stable_error_code"]
                == "connector_not_in_workspace"
            )

    asyncio.run(run())


def test_clerk_tools_list_omits_connectors_when_unbound(tmp_path: Path) -> None:
    from quail.mcp.server import create_clerk_mcp_server

    catalog = _acme_ping_catalog()
    manifest = _write_clerk_manifest(tmp_path, alice_default=None)
    config = load_config(manifest)
    apply_config(config).close()
    verifier = StaticTokenVerifier({"alice-token": "user_alice"})
    server = create_clerk_mcp_server(config, verifier=verifier, connector_catalog=catalog)

    async def run() -> None:
        with bearer_token("alice-token"):
            names = {tool.name for tool in await server.list_tools()}
            assert "garden_ping" not in names
            assert "quail_switch_workspace" in names
            switched = _as_dict(
                await server.call_tool("quail_switch_workspace", {"workspace_id": "acme"})
            )
            assert switched["active_workspace_id"] == "acme"
            names = {tool.name for tool in await server.list_tools()}
            assert "garden_ping" in names

    asyncio.run(run())
