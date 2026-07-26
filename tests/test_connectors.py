"""Connector SDK load, docs ownership, MCP wire, and example package."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

from quail.config.errors import ConfigError
from quail.config.models import (
    ConnectorBinding,
    ExtensionPin,
    QuailConfig,
    WorkspaceSpec,
)
from quail.config.parse import load_config
from quail.connectors.load import (
    ConnectorCatalog,
    ConnectorLoadError,
    load_connector_catalog,
    resolve_dataset_documentation,
)
from quail.connectors.load import load as load_module
from quail.connectors.mcp_wire import register_connectors
from quail.connectors.sdk import (
    ConnectorContext,
    ConnectorEnvironment,
    ConnectorError,
    ConnectorHost,
    ConnectorManifest,
    DatasetRef,
    ToolResult,
    ToolSpec,
    WidgetSpec,
    WorkspaceConnectorRuntime,
)
from quail.datasets import get_dataset, import_csv_dataset, open_core_db
from quail.mcp import create_mcp_server
from quail.mcp.results import classify_exception, diagnostic_from_exception


class _Host(ConnectorHost):
    def __init__(self, datasets: Mapping[str, DatasetRef]) -> None:
        self._datasets = dict(datasets)

    def dataset(self, context: ConnectorContext, dataset_id: str) -> DatasetRef:
        self.require_dataset(context, dataset_id)
        ref = self._datasets.get(dataset_id)
        if ref is None:
            raise PermissionError("The requested connector dataset is not available")
        return ref

    def require_dataset(self, context: ConnectorContext, dataset_id: str) -> None:
        context.require_dataset(dataset_id)


class _DocsProvider:
    def __init__(self, docs: Mapping[str, str | None]) -> None:
        self._docs = dict(docs)

    def call_tool(
        self,
        context: ConnectorContext,
        name: str,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        del context, arguments
        raise ConnectorError("UNKNOWN_TOOL", f"No tool {name}", "Use declared tools only.")

    def dataset_document(self, context: ConnectorContext, dataset_id: str) -> str | None:
        context.require_dataset(dataset_id)
        return self._docs.get(dataset_id)

    def handle_route(self, context, route_id, path_params):
        del context, route_id, path_params
        return None


class _ToolProvider:
    def __init__(self, host: ConnectorHost, label: str) -> None:
        self._host = host
        self._label = label

    def call_tool(
        self,
        context: ConnectorContext,
        name: str,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        if name != "probe_dataset":
            raise ConnectorError("UNKNOWN_TOOL", "bad tool", "retry")
        dataset_id = arguments["dataset_id"]
        assert isinstance(dataset_id, str)
        ref = self._host.dataset(context, dataset_id)
        return ToolResult(
            structured_content={
                "dataset_id": ref.id,
                "label": self._label,
                "name": ref.name,
            }
        )

    def dataset_document(self, context: ConnectorContext, dataset_id: str) -> str | None:
        if dataset_id != "sample":
            return None
        self._host.require_dataset(context, dataset_id)
        return f"Docs from {self._label} for {dataset_id}"

    def handle_route(self, context, route_id, path_params):
        del context, route_id, path_params
        return None


class _FakeConnector:
    def __init__(
        self,
        environment: ConnectorEnvironment,
        *,
        extension_id: str,
        version: str,
        docs: Mapping[str, str | None] | None = None,
        with_tool: bool = False,
        with_widget: bool = False,
        label: str = "alpha",
    ) -> None:
        self._environment = environment
        self._docs = dict(docs or {})
        self._with_tool = with_tool
        self._label = label
        tools = ()
        if with_tool:
            tools = (
                ToolSpec(
                    name="probe_dataset",
                    title="Probe dataset",
                    description="Return a small probe card.",
                    input_schema={
                        "type": "object",
                        "properties": {"dataset_id": {"type": "string"}},
                        "required": ["dataset_id"],
                        "additionalProperties": False,
                    },
                ),
            )
        widgets = ()
        if with_widget:
            widgets = (
                WidgetSpec(
                    id="probe_card",
                    uri="ui://probe/card.html",
                ),
            )
        self._manifest = ConnectorManifest(
            id=extension_id,
            version=version,
            tools=tools,
            widgets=widgets,
            config_keys=frozenset({"heading"}),
        )

    @property
    def manifest(self) -> ConnectorManifest:
        return self._manifest

    def read_resource(self, uri: str) -> str:
        if uri == "ui://probe/card.html":
            return "<html><body>probe</body></html>"
        raise ValueError(uri)

    def connect(self, runtime: WorkspaceConnectorRuntime) -> Any:
        if self._with_tool:
            return _ToolProvider(self._environment.host, self._label)
        return _DocsProvider(self._docs)


def _config(
    tmp_path: Path,
    *,
    extensions: tuple[ExtensionPin, ...],
    connectors: tuple[ConnectorBinding, ...],
    workspace_id: str = "local",
) -> QuailConfig:
    csv_path = tmp_path / "notes.csv"
    if not csv_path.exists():
        csv_path.write_text("id,title,content\ne1,Hello,body\n", encoding="utf-8")
    sample = tmp_path / "sample.csv"
    if not sample.exists():
        sample.write_text(
            "id,title,body\n"
            "s1,Cover,hello\n",
            encoding="utf-8",
        )
    manifest = tmp_path / "quail.toml"
    return QuailConfig(
        manifest_path=manifest,
        database=tmp_path / "core.turso",
        feedback=tmp_path / "feedback.jsonl",
        auth_mode="unrestricted",
        bind="127.0.0.1",
        port=8765,
        public_base_url="http://127.0.0.1:8765",
        workspace_id=workspace_id,
        clerk_domain=None,
        clerk_authorized_parties=(),
        workspaces=(
            WorkspaceSpec(
                workspace_id=workspace_id,
                datasets=(),
                connectors=connectors,
            ),
        ),
        users=(),
        datasets=(),
        extensions=extensions,
    )


def _seed_db(tmp_path: Path, workspace_id: str = "local") -> Path:
    db_path = tmp_path / "core.turso"
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text(
        "id,title,body\n"
        "s1,Cover,hello\n",
        encoding="utf-8",
    )
    with open_core_db(db_path) as db:
        import_csv_dataset(db, workspace_id, "sample", csv_path, activate=True)
        import_csv_dataset(
            db,
            workspace_id,
            "notes",
            tmp_path / "notes.csv"
            if (tmp_path / "notes.csv").exists()
            else _write_notes(tmp_path),
            activate=True,
        )
    return db_path


def _write_notes(tmp_path: Path) -> Path:
    path = tmp_path / "notes.csv"
    path.write_text("id,title,content\ne1,Hello,body\n", encoding="utf-8")
    return path


def _install_factory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    extension_id: str,
    version: str,
    factory: Any,
) -> None:
    def fake_load(
        pins: tuple[ExtensionPin, ...],
        *,
        base_path: Path,
    ) -> dict[str, tuple[Any, str]]:
        del base_path
        loaded: dict[str, tuple[Any, str]] = {}
        for pin in pins:
            if pin.extension_id != extension_id:
                raise ConnectorLoadError(f"unexpected pin {pin.extension_id}")
            if pin.version != version:
                raise ConnectorLoadError(
                    f"Connector {pin.extension_id!r} installed version {version!r} "
                    f"does not match TOML pin {pin.version!r}"
                )
            loaded[pin.extension_id] = (factory, version)
        return loaded

    monkeypatch.setattr(load_module, "_load_pinned_packages", fake_load)


def test_version_mismatch_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(environment: ConnectorEnvironment) -> _FakeConnector:
        return _FakeConnector(environment, extension_id="alpha", version="1.0.0")

    _install_factory(monkeypatch, extension_id="alpha", version="1.0.0", factory=factory)
    config = _config(
        tmp_path,
        extensions=(ExtensionPin("alpha", "9.9.9"),),
        connectors=(ConnectorBinding("alpha", {}, ("sample",)),),
    )
    with open_core_db(_seed_db(tmp_path)) as db:
        with pytest.raises(ConnectorLoadError, match="does not match TOML pin"):
            load_connector_catalog(config, db)


def test_doc_ownership_conflict_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factories: dict[str, Any] = {}

    def make_factory(extension_id: str) -> Any:
        def factory(environment: ConnectorEnvironment) -> _FakeConnector:
            return _FakeConnector(
                environment,
                extension_id=extension_id,
                version="1.0.0",
                docs={"sample": f"docs from {extension_id}"},
            )

        return factory

    factories["alpha"] = make_factory("alpha")
    factories["beta"] = make_factory("beta")

    def fake_load(
        pins: tuple[ExtensionPin, ...],
        *,
        base_path: Path,
    ) -> dict[str, tuple[Any, str]]:
        del base_path
        return {pin.extension_id: (factories[pin.extension_id], pin.version) for pin in pins}

    monkeypatch.setattr(load_module, "_load_pinned_packages", fake_load)
    config = _config(
        tmp_path,
        extensions=(ExtensionPin("alpha", "1.0.0"), ExtensionPin("beta", "1.0.0")),
        connectors=(
            ConnectorBinding("alpha", {}, ("sample",)),
            ConnectorBinding("beta", {}, ("sample",)),
        ),
    )
    with open_core_db(_seed_db(tmp_path)) as db:
        with pytest.raises(ConnectorLoadError, match="Multiple connectors provide dataset"):
            load_connector_catalog(config, db)


def test_workspace_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(environment: ConnectorEnvironment) -> _FakeConnector:
        return _FakeConnector(
            environment,
            extension_id="alpha",
            version="1.0.0",
            docs={"sample": "only in ws-a"},
        )

    def fake_load(
        pins: tuple[ExtensionPin, ...],
        *,
        base_path: Path,
    ) -> dict[str, tuple[Any, str]]:
        del base_path
        return {pin.extension_id: (factory, pin.version) for pin in pins}

    monkeypatch.setattr(load_module, "_load_pinned_packages", fake_load)

    csv_a = tmp_path / "a.csv"
    csv_a.write_text(
        "id,issue,page,type,title,author,content,asset_ref\ng1,170,1,text,Cover,,hello,\n",
        encoding="utf-8",
    )
    csv_b = tmp_path / "b.csv"
    csv_b.write_text(
        "id,issue,page,type,title,author,content,asset_ref\ng1,170,1,text,Cover,,hello,\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "core.turso"
    with open_core_db(db_path) as db:
        import_csv_dataset(db, "ws-a", "sample", csv_a, activate=True)
        import_csv_dataset(db, "ws-b", "sample", csv_b, activate=True)

    config = QuailConfig(
        manifest_path=tmp_path / "quail.toml",
        database=db_path,
        feedback=tmp_path / "feedback.jsonl",
        auth_mode="clerk",
        bind="127.0.0.1",
        port=8765,
        public_base_url="http://127.0.0.1:8765",
        workspace_id=None,
        clerk_domain="example.clerk.accounts.dev",
        clerk_authorized_parties=("test_clerk_app",),
        workspaces=(
            WorkspaceSpec(
                workspace_id="ws-a",
                datasets=(),
                connectors=(ConnectorBinding("alpha", {}, ("sample",)),),
            ),
            WorkspaceSpec(workspace_id="ws-b", datasets=(), connectors=()),
        ),
        users=(),
        datasets=(),
        extensions=(ExtensionPin("alpha", "1.0.0"),),
    )
    with open_core_db(db_path) as db:
        catalog = load_connector_catalog(config, db)
    assert "sample" in catalog.for_workspace("ws-a").docs_by_dataset
    assert catalog.for_workspace("ws-b").docs_by_dataset == {}
    assert catalog.for_workspace("ws-b").providers == ()


def test_dataset_document_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(environment: ConnectorEnvironment) -> _FakeConnector:
        return _FakeConnector(
            environment,
            extension_id="alpha",
            version="1.0.0",
            with_tool=True,
            label="alpha",
        )

    _install_factory(monkeypatch, extension_id="alpha", version="1.0.0", factory=factory)
    config = _config(
        tmp_path,
        extensions=(ExtensionPin("alpha", "1.0.0"),),
        connectors=(ConnectorBinding("alpha", {"heading": "Sample"}, ("sample",)),),
    )
    db_path = _seed_db(tmp_path)
    with open_core_db(db_path) as db:
        catalog = load_connector_catalog(config, db)
        text = resolve_dataset_documentation(
            catalog.for_workspace("local"),
            workspace_id="local",
            user_id=None,
            dataset_id="sample",
        )
    assert text is not None
    assert "Docs from alpha" in text
    assert (
        resolve_dataset_documentation(
            catalog.for_workspace("local"),
            workspace_id="local",
            user_id=None,
            dataset_id="notes",
        )
        is None
    )


def test_mcp_tool_and_widget_and_dataset_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(environment: ConnectorEnvironment) -> _FakeConnector:
        return _FakeConnector(
            environment,
            extension_id="alpha",
            version="1.0.0",
            with_tool=True,
            with_widget=True,
            label="alpha",
        )

    _install_factory(monkeypatch, extension_id="alpha", version="1.0.0", factory=factory)
    db_path = _seed_db(tmp_path)
    config = _config(
        tmp_path,
        extensions=(ExtensionPin("alpha", "1.0.0"),),
        connectors=(ConnectorBinding("alpha", {}, ("sample",)),),
    )
    with open_core_db(db_path) as db:
        catalog = load_connector_catalog(config, db)
    feedback = tmp_path / "feedback.jsonl"
    server = create_mcp_server(
        db_path,
        feedback,
        workspace_id="local",
        connector_catalog=catalog,
    )

    async def run() -> None:
        tools = {tool.name for tool in await server.list_tools()}
        assert "probe_dataset" in tools
        resources = {str(resource.uri) for resource in await server.list_resources()}
        assert "ui://probe/card.html" in resources
        probe = await server.call_tool("probe_dataset", {"dataset_id": "sample"})
        assert isinstance(probe, CallToolResult)
        assert not probe.isError
        assert probe.structuredContent is not None
        assert probe.structuredContent["label"] == "alpha"
        info = await server.call_tool("quail_get_dataset_info", {"dataset_id": "sample"})
        assert isinstance(info, CallToolResult)
        assert not info.isError
        assert info.structuredContent is not None
        assert "Docs from alpha" in info.structuredContent["documentation"]
        body = list(await server.read_resource("ui://probe/card.html"))
        assert "probe" in str(body[0].content)

    asyncio.run(run())


def test_parse_extensions_and_connectors(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "notes.csv").write_text("id,title\ne1,Hello\n", encoding="utf-8")
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
port = 8765
public_base_url = "http://127.0.0.1:8765"

[[datasets]]
id = "notes"
source = "data/notes.csv"

[[extensions]]
id = "alpha"
version = "1.0.0"

[[connectors]]
id = "alpha"

[connectors.config]
heading = "Hello"

[[connectors.datasets]]
id = "notes"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(manifest)
    assert config.extensions == (ExtensionPin("alpha", "1.0.0"),)
    assert config.workspaces[0].connectors[0].extension_id == "alpha"
    assert config.workspaces[0].connectors[0].config == {"heading": "Hello"}
    assert config.workspaces[0].connectors[0].dataset_ids == ("notes",)


def test_parse_rejects_unpinned_connector(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "notes.csv").write_text("id,title\ne1,Hello\n", encoding="utf-8")
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
port = 8765
public_base_url = "http://127.0.0.1:8765"

[[datasets]]
id = "notes"
source = "data/notes.csv"

[[connectors]]
id = "missing"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="not pinned"):
        load_config(manifest)


def test_connector_error_classification() -> None:
    error = ConnectorError("PIN_MISMATCH", "bad pin", "fix the TOML pin")
    assert classify_exception(error) == ("ConnectorError", "pin_mismatch")
    diagnostic = diagnostic_from_exception(error)
    assert diagnostic["repair_hint"] == "fix the TOML pin"


def test_example_package_surface(tmp_path: Path) -> None:
    example_src = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "notes-connector"
        / "src"
    )
    sys.path.insert(0, str(example_src))
    try:
        from quail_notes_connector import create_connector
    finally:
        sys.path.remove(str(example_src))

    db_path = _seed_db(tmp_path)
    with open_core_db(db_path) as db:
        host = _CoreHostAdapter(db)
        environment = ConnectorEnvironment(
            host=host,
            public_base_url="http://127.0.0.1:8765",
            base_path=tmp_path,
        )
        connector = create_connector(environment)
        assert connector.manifest.id == "notes"
        assert connector.manifest.version == "1.0.0"
        assert any(t.name == "notes_describe_dataset" for t in connector.manifest.tools)
        assert any(w.uri.startswith("ui://") for w in connector.manifest.widgets)
        provider = connector.connect(
            WorkspaceConnectorRuntime(
                workspace_id="local",
                config={"heading": "Notes"},
                dataset_ids=frozenset({"notes"}),
            )
        )
        context = ConnectorContext(
            workspace_id="local",
            user_id=None,
            extension_id="notes",
            extension_version="1.0.0",
            dataset_ids=frozenset({"notes"}),
        )
        docs = provider.dataset_document(context, "notes")
        assert docs is not None
        assert "Lexical" in docs
        result = provider.call_tool(context, "notes_describe_dataset", {"dataset_id": "notes"})
        assert isinstance(result, ToolResult)
        assert result.structured_content["heading"] == "Notes"
        html = connector.read_resource("ui://notes/dataset-card.html")
        assert "notes_describe_dataset" in html


class _CoreHostAdapter:
    def __init__(self, db: Any) -> None:
        self._db = db

    def dataset(self, context: ConnectorContext, dataset_id: str) -> DatasetRef:
        context.require_dataset(dataset_id)
        ref = get_dataset(self._db, context.workspace_id, dataset_id)
        if ref is None:
            raise PermissionError("missing")
        return DatasetRef(
            id=ref.dataset_id,
            name=ref.name,
            active_version_id=ref.active_version_id,
        )

    def require_dataset(self, context: ConnectorContext, dataset_id: str) -> None:
        context.require_dataset(dataset_id)


def test_register_connectors_noop_catalog() -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("t")
    register_connectors(
        server,
        ConnectorCatalog(),
        resolve_workspace=lambda _ctx: ("local", None),
    )


def test_connector_tools_list_publishes_input_schema_and_hides_context() -> None:
    from mcp.server.fastmcp import FastMCP

    from quail.connectors.load import ConnectedProvider, WorkspaceConnectorBundle

    input_schema = {
        "type": "object",
        "properties": {
            "asset_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 8,
                "uniqueItems": True,
            },
        },
        "required": ["asset_ids"],
        "additionalProperties": False,
    }

    class _SchemaProvider:
        def call_tool(self, context, name, arguments):
            del context, name, arguments
            return ToolResult(structured_content={"ok": True})

        def dataset_document(self, context, dataset_id):
            del context, dataset_id
            return None

        def handle_route(self, context, route_id, path_params):
            del context, route_id, path_params
            return None

    class _SchemaConnector:
        def __init__(self) -> None:
            self._manifest = ConnectorManifest(
                id="schema_demo",
                version="1.0.0",
                tools=(
                    ToolSpec(
                        name="schema_demo_show",
                        title="Show",
                        description="Demo tool with a rich input schema.",
                        input_schema=input_schema,
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
            return _SchemaProvider()

    connector = _SchemaConnector()
    connected = ConnectedProvider(
        extension_id="schema_demo",
        version="1.0.0",
        manifest=connector.manifest,
        connector=connector,
        provider=_SchemaProvider(),
        dataset_ids=frozenset({"sample"}),
        provides_docs=False,
    )
    catalog = ConnectorCatalog(
        by_workspace={
            "local": WorkspaceConnectorBundle(
                workspace_id="local",
                providers=(connected,),
                docs_by_dataset={},
            )
        }
    )
    server = FastMCP("t")
    register_connectors(
        server,
        catalog,
        resolve_workspace=lambda _ctx: ("local", None),
    )

    async def run() -> None:
        tools = {tool.name: tool for tool in await server.list_tools()}
        tool = tools["schema_demo_show"]
        assert tool.inputSchema == input_schema
        assert "ctx" not in tool.inputSchema.get("properties", {})
        registered = server._tool_manager.get_tool("schema_demo_show")
        assert registered is not None
        assert registered.context_kwarg == "ctx"

    asyncio.run(run())


def test_connector_file_route_serves_bytes(tmp_path: Path) -> None:
    from mcp.server.fastmcp import FastMCP
    from starlette.testclient import TestClient

    from quail.connectors.load import ConnectedProvider, WorkspaceConnectorBundle
    from quail.connectors.sdk import FileResponse, RouteSpec

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
            if context.workspace_id != "garden":
                return None
            return FileResponse(asset, "image/png", filename="page.png", expires_at=9_999_999_999)

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
        dataset_ids=frozenset({"garden-gate"}),
        provides_docs=False,
    )
    catalog = ConnectorCatalog(
        by_workspace={
            "garden": WorkspaceConnectorBundle(
                workspace_id="garden",
                providers=(connected,),
                docs_by_dataset={},
            )
        }
    )
    server = FastMCP("t")
    register_connectors(
        server,
        catalog,
        resolve_workspace=lambda _ctx: ("garden", None),
    )
    app = server.streamable_http_app()
    client = TestClient(app)
    missing = client.get("/extensions/garden_gate/other/assets/good")
    assert missing.status_code == 404
    bad = client.get("/extensions/garden_gate/garden/assets/bad")
    assert bad.status_code == 404
    ok = client.get("/extensions/garden_gate/garden/assets/good")
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("image/png")
    assert ok.content.startswith(b"\x89PNG")


def test_unknown_config_keys_rejected_even_with_empty_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EmptyKeysConnector(_FakeConnector):
        def __init__(self, environment: ConnectorEnvironment) -> None:
            super().__init__(environment, extension_id="alpha", version="1.0.0")
            object.__setattr__(
                self,
                "_manifest",
                ConnectorManifest(
                    id="alpha",
                    version="1.0.0",
                    config_keys=frozenset(),
                ),
            )

    def factory(environment: ConnectorEnvironment) -> EmptyKeysConnector:
        return EmptyKeysConnector(environment)

    _install_factory(monkeypatch, extension_id="alpha", version="1.0.0", factory=factory)
    config = _config(
        tmp_path,
        extensions=(ExtensionPin("alpha", "1.0.0"),),
        connectors=(ConnectorBinding("alpha", {"stray": True}, ("sample",)),),
    )
    with open_core_db(_seed_db(tmp_path)) as db:
        with pytest.raises(ConnectorLoadError, match="unknown config keys"):
            load_connector_catalog(config, db)


def test_tool_name_conflict_across_connectors() -> None:
    from mcp.server.fastmcp import FastMCP

    from quail.connectors.load import ConnectedProvider, WorkspaceConnectorBundle

    env = ConnectorEnvironment(
        host=_Host({}),
        public_base_url="http://127.0.0.1:8765",
        base_path=Path("/tmp"),
    )
    alpha = _FakeConnector(env, extension_id="alpha", version="1.0.0", with_tool=True)
    beta = _FakeConnector(env, extension_id="beta", version="1.0.0", with_tool=True)
    catalog = ConnectorCatalog(
        by_workspace={
            "local": WorkspaceConnectorBundle(
                workspace_id="local",
                providers=(
                    ConnectedProvider(
                        extension_id="alpha",
                        version="1.0.0",
                        manifest=alpha.manifest,
                        connector=alpha,
                        provider=_ToolProvider(env.host, "a"),
                        dataset_ids=frozenset({"sample"}),
                        provides_docs=False,
                    ),
                    ConnectedProvider(
                        extension_id="beta",
                        version="1.0.0",
                        manifest=beta.manifest,
                        connector=beta,
                        provider=_ToolProvider(env.host, "b"),
                        dataset_ids=frozenset({"sample"}),
                        provides_docs=False,
                    ),
                ),
                docs_by_dataset={},
            )
        }
    )
    server = FastMCP("conflict")
    with pytest.raises(ConnectorError, match="TOOL_NAME_CONFLICT|claimed"):
        register_connectors(
            server,
            catalog,
            resolve_workspace=lambda _ctx: ("local", None),
        )


def test_tool_result_text_not_merged_into_structured() -> None:
    from quail.mcp.results import success_result

    result = success_result({"assets": [{"id": "a"}]}, text="two previews")
    assert result.structuredContent == {"assets": [{"id": "a"}]}
    assert result.content[0].text == "two previews"
    assert "text" not in result.structuredContent
