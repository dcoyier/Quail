"""Thin MCP adapter: retrieval surface + separate feedback store."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Sequence

import pytest
from mcp.types import CallToolResult

from quail.config import load_config
from quail.datasets import import_csv_dataset, open_core_db
from quail.mcp import create_mcp_server
from quail.providers import EmbeddingClient
from quail.run import process_config
from quail.search.pool import open_search_pool
from quail.search.runtime import SearchRuntime
from quail.session import get_session


class _FakeEmbedder:
    """Deterministic bag-of-chars embedder for MCP search tests."""

    def __init__(self, *, dimensions: int = 4) -> None:
        self.dimensions = dimensions

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for index, char in enumerate(text.lower()):
                vector[index % self.dimensions] += float(ord(char) % 31) + 1.0
            vectors.append(vector)
        return vectors


def _seed(tmp_path: Path):
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,title,body\ne1,Hello,hydrangea care tips\ne2,Other,climate notes\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "core.turso"
    feedback_path = tmp_path / "feedback.jsonl"
    with open_core_db(db_path) as db:
        import_csv_dataset(db, "local", "notes", csv_path, activate=True)
    server = create_mcp_server(db_path, feedback_path, workspace_id="local")
    return server, db_path, feedback_path


def _seed_with_search(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "notes.csv").write_text(
        "id,title,body\ne1,Hello,hydrangea care tips\ne2,Other,climate notes\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        """
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"
search_database = "data/quail-search.turso"

[auth]
mode = "unrestricted"
workspace = "local"

[hosting]
bind = "127.0.0.1"
port = 8000

[providers.ollama]
base_url = "http://127.0.0.1:11434"

[[datasets]]
id = "notes"
source = "data/notes.csv"

[datasets.embedding]
provider = "ollama"
model = "fake"
dimensions = 4
revision = "test-v1"
""",
        encoding="utf-8",
    )
    config = load_config(manifest)
    fake: EmbeddingClient = _FakeEmbedder(dimensions=4)
    process_config(config, embedder_factory=lambda _profile: fake)
    assert config.search_database is not None
    runtime = SearchRuntime(
        path=config.search_database,
        providers=config.providers,
        pool=open_search_pool(config.search_database, max_size=2),
        embedder_factory=lambda _profile: _FakeEmbedder(dimensions=4),
    )
    server = create_mcp_server(
        config.database,
        config.feedback,
        workspace_id="local",
        search_runtime=runtime,
    )
    return server, runtime


async def _call(server, name: str, arguments: dict | None = None):
    return await server.call_tool(name, arguments or {})


def _as_dict(result) -> dict:
    if isinstance(result, CallToolResult):
        assert result.structuredContent is not None
        return dict(result.structuredContent)
    if isinstance(result, dict):
        return result
    if hasattr(result, "keys"):
        return dict(result)
    raise AssertionError(f"Unexpected tool result type: {type(result)!r}")


def _is_error(result) -> bool:
    return isinstance(result, CallToolResult) and bool(result.isError)


def test_server_instructions_and_tool_definitions(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)
    assert server.instructions is not None
    assert "workspace `local`" in server.instructions
    assert "quail_setup" in server.instructions
    assert "quail_get_dataset_info" in server.instructions
    assert "search" in server.instructions
    assert "get_entry" in server.instructions
    assert "quail_get_api_docs" not in server.instructions
    assert "quail_exec" not in server.instructions
    assert "provide_feedback" in server.instructions
    assert "session_id" in (server.instructions or "")

    async def run() -> None:
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert set(tools) == {
            "quail_setup",
            "quail_list_datasets",
            "quail_start_session",
            "quail_get_dataset_info",
            "search",
            "get_entry",
            "provide_feedback",
        }
        assert "quail_exec" not in tools
        assert "quail_get_api_docs" not in tools
        assert "cold-start" in (tools["quail_setup"].description or "").lower()
        assert "dataset_id" in tools["search"].inputSchema["properties"]
        assert "query" in tools["search"].inputSchema["properties"]
        assert "top_k" in tools["search"].inputSchema["properties"]
        assert "result_handle" in tools["get_entry"].inputSchema["properties"]
        assert "entry_id" not in tools["get_entry"].inputSchema["properties"]
        assert "dataset_id" not in tools["get_entry"].inputSchema["properties"]
        assert "message" in tools["provide_feedback"].inputSchema["properties"]

    asyncio.run(run())


def test_quail_setup_returns_catalog_and_session_without_api_docs(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        setup = _as_dict(await _call(server, "quail_setup"))
        assert setup["workspace_id"] == "local"
        assert setup["state_revision"] == 0
        assert setup["session_id"]
        assert "documentation" not in setup
        assert setup["datasets"][0]["dataset_id"] == "notes"
        assert setup["datasets"][0]["active_version_id"]
        assert "documentation" not in setup["datasets"][0]

    asyncio.run(run())


def test_quail_setup_can_include_dataset_docs(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,title,body\ne1,Hello,hydrangea care tips\ne2,Other,climate notes\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "core.turso"
    feedback_path = tmp_path / "feedback.jsonl"
    with open_core_db(db_path) as db:
        import_csv_dataset(db, "local", "notes", csv_path, activate=True)
    server = create_mcp_server(
        db_path,
        feedback_path,
        workspace_id="local",
        include_dataset_docs_in_setup=True,
    )

    async def run() -> None:
        setup = _as_dict(await _call(server, "quail_setup"))
        assert "documentation" in setup["datasets"][0]
        assert "search" in setup["datasets"][0]["documentation"]
        assert "quail_exec" not in setup["datasets"][0]["documentation"]

    asyncio.run(run())


def test_list_and_dataset_info(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        listed = _as_dict(await _call(server, "quail_list_datasets"))
        assert listed["datasets"][0]["dataset_id"] == "notes"
        assert listed["datasets"][0]["active_version_id"]
        info = _as_dict(await _call(server, "quail_get_dataset_info", {"dataset_id": "notes"}))
        assert info["dataset_id"] == "notes"
        assert "search" in info["documentation"]
        assert "quail_exec" not in info["documentation"]

    asyncio.run(run())


def test_search_and_get_entry_round_trip(tmp_path: Path) -> None:
    server, runtime = _seed_with_search(tmp_path)
    try:

        async def run() -> None:
            started = _as_dict(await _call(server, "quail_start_session"))
            session_id = started["session_id"]
            result = _as_dict(
                await _call(
                    server,
                    "search",
                    {
                        "session_id": session_id,
                        "dataset_id": "notes",
                        "query": "hydrangea care",
                        "top_k": 2,
                    },
                )
            )
            assert result["query"] == "hydrangea care"
            assert result["top_k"] == 2
            assert len(result["hits"]) >= 1
            assert result["hits"][0]["rank"] == 1
            assert "entry_id" in result["hits"][0]
            assert "result_handle" in result["hits"][0]
            assert "text" in result["hits"][0]
            entry_id = result["hits"][0]["entry_id"]
            result_handle = result["hits"][0]["result_handle"]
            entry = _as_dict(
                await _call(
                    server,
                    "get_entry",
                    {
                        "session_id": session_id,
                        "result_handle": result_handle,
                    },
                )
            )
            assert entry["entry_id"] == entry_id
            assert "body" in entry["fields"]
            assert "title" in entry["fields"]

        asyncio.run(run())
    finally:
        runtime.close()


def test_search_rejects_bad_top_k(tmp_path: Path) -> None:
    server, runtime = _seed_with_search(tmp_path)
    try:

        async def run() -> None:
            started = _as_dict(await _call(server, "quail_start_session"))
            result = await _call(
                server,
                "search",
                {
                    "session_id": started["session_id"],
                    "dataset_id": "notes",
                    "query": "hydrangea",
                    "top_k": 99,
                },
            )
            assert _is_error(result)
            payload = _as_dict(result)
            assert payload["diagnostic"]["error_class"] == "QuailSyntaxError"

        asyncio.run(run())
    finally:
        runtime.close()


def test_search_unknown_dataset_error_class(tmp_path: Path) -> None:
    server, runtime = _seed_with_search(tmp_path)
    try:

        async def run() -> None:
            started = _as_dict(await _call(server, "quail_start_session"))
            result = await _call(
                server,
                "search",
                {
                    "session_id": started["session_id"],
                    "dataset_id": "no-such-dataset",
                    "query": "hydrangea",
                },
            )
            assert _is_error(result)
            payload = _as_dict(result)
            assert payload["diagnostic"]["error_class"] == "QuailScopeError"

        asyncio.run(run())
    finally:
        runtime.close()


def test_get_entry_rejects_raw_or_invented_entry_id(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        started = _as_dict(await _call(server, "quail_start_session"))
        result = await _call(
            server,
            "get_entry",
            {
                "session_id": started["session_id"],
                "result_handle": "basking/Baskinginreflectedglory_4.txt",
            },
        )
        assert _is_error(result)
        payload = _as_dict(result)
        assert payload["diagnostic"]["error_class"] == "QuailScopeError"
        assert "rerun search" in payload["diagnostic"]["repair_hint"].lower()

    asyncio.run(run())


def test_get_entry_rejects_handle_from_another_session(tmp_path: Path) -> None:
    server, runtime = _seed_with_search(tmp_path)
    try:

        async def run() -> None:
            first = _as_dict(await _call(server, "quail_start_session"))
            second = _as_dict(await _call(server, "quail_start_session"))
            searched = _as_dict(
                await _call(
                    server,
                    "search",
                    {
                        "session_id": first["session_id"],
                        "dataset_id": "notes",
                        "query": "hydrangea",
                    },
                )
            )
            result = await _call(
                server,
                "get_entry",
                {
                    "session_id": second["session_id"],
                    "result_handle": searched["hits"][0]["result_handle"],
                },
            )
            assert _is_error(result)
            payload = _as_dict(result)
            assert payload["diagnostic"]["error_class"] == "QuailScopeError"

        asyncio.run(run())
    finally:
        runtime.close()


def test_provide_feedback_appends_jsonl(tmp_path: Path) -> None:
    server, db_path, feedback_path = _seed(tmp_path)
    core_before = db_path.read_bytes()

    async def run() -> None:
        result = _as_dict(
            await _call(
                server,
                "provide_feedback",
                {
                    "message": "Dataset list was clear but docs felt long.",
                    "category": "docs",
                },
            )
        )
        assert result == {"accepted": True}

    asyncio.run(run())
    lines = feedback_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["workspace_id"] == "local"
    assert record["message"].startswith("Dataset list")
    assert record["category"] == "docs"
    assert record["session_id"] is None
    assert record["dataset_id"] is None
    assert "timestamp" in record
    assert db_path.read_bytes() == core_before


def test_provide_feedback_rejects_oversized_message(tmp_path: Path) -> None:
    from quail.mcp.feedback import append_feedback

    path = tmp_path / "feedback.jsonl"
    with pytest.raises(ValueError, match="16|16384|bytes"):
        append_feedback(
            path,
            workspace_id="local",
            message="x" * (16 * 1024 + 1),
        )
    assert not path.exists()


def test_provide_feedback_rejects_when_file_at_quota(tmp_path: Path) -> None:
    from quail.mcp.feedback import append_feedback
    from quail.mcp.feedback import feedback as feedback_mod

    path = tmp_path / "feedback.jsonl"
    path.write_bytes(b"x" * feedback_mod._MAX_FILE_BYTES)
    with pytest.raises(ValueError, match="64|file exceeds|quota"):
        append_feedback(path, workspace_id="local", message="one more note")


def test_prepared_mcp_close_closes_search_pool_and_connectors(tmp_path: Path) -> None:
    from quail.config.models import ProvidersConfig
    from quail.mcp.server import PreparedMcp
    from quail.search import open_search_db

    class _Catalog:
        closed = False

        def close(self) -> None:
            self.closed = True

    path = tmp_path / "search.turso"
    open_search_db(path).close()
    runtime = SearchRuntime(
        path=path,
        providers=ProvidersConfig(),
        pool=open_search_pool(path, max_size=1),
    )
    handle = runtime.pool.checkout()
    runtime.pool.release(handle)
    catalog = _Catalog()
    prepared = PreparedMcp(server=object(), search_runtime=runtime, connector_catalog=catalog)  # type: ignore[arg-type]
    prepared.close()
    assert catalog.closed is True
    with pytest.raises(RuntimeError, match="closed"):
        runtime.pool.checkout()


def test_search_without_runtime_fails_closed(tmp_path: Path) -> None:
    server, db_path, _ = _seed(tmp_path)

    async def run() -> None:
        started = _as_dict(await _call(server, "quail_start_session"))
        result = await _call(
            server,
            "search",
            {
                "session_id": started["session_id"],
                "dataset_id": "notes",
                "query": "hydrangea",
            },
        )
        assert _is_error(result)
        with open_core_db(db_path) as db:
            session = get_session(db, started["session_id"])
            assert session is not None and session.state_revision == 0

    asyncio.run(run())
