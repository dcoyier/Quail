"""Thin MCP adapter: agent loop + separate feedback store."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp.types import CallToolResult

from quail.datasets import import_csv_dataset, open_core_db
from quail.mcp import create_mcp_server
from quail.session import catalog_fields, get_session, resolve_scope


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


async def _call(server, name: str, arguments: dict | None = None):
    return await server.call_tool(name, arguments or {})


def _as_dict(result) -> dict:
    if isinstance(result, CallToolResult):
        assert result.structuredContent is not None
        return dict(result.structuredContent)
    if isinstance(result, dict):
        return result
    # FastMCP may unwrap structured content to a plain mapping-like object
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
    assert "quail_get_api_docs" in server.instructions
    assert "provide_feedback" in server.instructions
    assert "session_id" in (server.instructions or "")

    async def run() -> None:
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert set(tools) == {
            "quail_setup",
            "quail_get_api_docs",
            "quail_list_datasets",
            "quail_start_session",
            "quail_get_dataset_info",
            "quail_exec",
            "provide_feedback",
        }
        assert "cold-start" in (tools["quail_setup"].description or "").lower()
        assert "analysis-language" in (tools["quail_get_api_docs"].description or "").lower()
        assert "dataset_id" in tools["quail_exec"].inputSchema["properties"]
        assert "message" in tools["provide_feedback"].inputSchema["properties"]

    asyncio.run(run())


def test_quail_setup_returns_docs_catalog_and_session(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        setup = _as_dict(await _call(server, "quail_setup"))
        assert setup["workspace_id"] == "local"
        assert setup["state_revision"] == 0
        assert setup["session_id"]
        assert "Quail Analysis API" in setup["documentation"]
        assert setup["datasets"][0]["dataset_id"] == "notes"
        assert setup["datasets"][0]["active_version_id"]
        assert "documentation" not in setup["datasets"][0]
        outcome = _as_dict(
            await _call(
                server,
                "quail_exec",
                {
                    "session_id": setup["session_id"],
                    "dataset_id": "notes",
                    "code": "print(count())\n",
                },
            )
        )
        assert "2" in outcome["printed_output"]

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
        assert "quail_exec" in setup["datasets"][0]["documentation"]

    asyncio.run(run())


def test_quail_get_api_docs(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        result = _as_dict(await _call(server, "quail_get_api_docs"))
        assert "Quail Analysis API" in result["documentation"]
        assert "provide_feedback(message" not in result["documentation"]
        assert "Feedback is stored outside" not in result["documentation"]

    asyncio.run(run())


def test_list_and_dataset_info(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        listed = _as_dict(await _call(server, "quail_list_datasets"))
        assert listed["datasets"][0]["dataset_id"] == "notes"
        assert listed["datasets"][0]["active_version_id"]
        info = _as_dict(await _call(server, "quail_get_dataset_info", {"dataset_id": "notes"}))
        assert info["dataset_id"] == "notes"
        assert "quail_exec" in info["documentation"]

    asyncio.run(run())


def test_start_session_and_exec_start_here(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        started = _as_dict(await _call(server, "quail_start_session"))
        session_id = started["session_id"]
        assert started["state_revision"] == 0
        code = """
for field in retrieve(unit=fields, group=G1, limit=50):
    print(field.name, field.kind)
samples = retrieve(limit=1)
sample = samples[0]
for field in sample.fields():
    print(field.name, repr(sample.value(field)))
"""
        outcome = _as_dict(
            await _call(
                server,
                "quail_exec",
                {
                    "session_id": session_id,
                    "dataset_id": "notes",
                    "code": code,
                },
            )
        )
        assert "title source" in outcome["printed_output"]
        assert "title 'Hello'" in outcome["printed_output"]

    asyncio.run(run())


def test_failed_exec_returns_diagnostic_without_overlay(tmp_path: Path) -> None:
    server, db_path, _ = _seed(tmp_path)

    async def run() -> None:
        started = _as_dict(await _call(server, "quail_start_session"))
        session_id = started["session_id"]
        result = await _call(
            server,
            "quail_exec",
            {
                "session_id": session_id,
                "dataset_id": "notes",
                "code": 'create_field("topic")\nprint(1 / 0)\n',
            },
        )
        assert _is_error(result)
        payload = _as_dict(result)
        assert payload["execution_id"] is None
        assert payload["diagnostic"]["error_class"]
        assert payload["diagnostic"]["stable_error_code"]
        assert payload["diagnostic"]["message"]
        with open_core_db(db_path) as db:
            session = get_session(db, session_id)
            assert session is not None and session.state_revision == 0
            scope = resolve_scope(db, session_id, "notes")
            assert all(field.kind == "source" for field in catalog_fields(db, scope))

    asyncio.run(run())


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
    from quail.search.pool import open_search_pool
    from quail.search.runtime import SearchRuntime

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


def test_exec_field_kind_mismatch_error_class(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        started = _as_dict(await _call(server, "quail_start_session"))
        result = await _call(
            server,
            "quail_exec",
            {
                "session_id": started["session_id"],
                "dataset_id": "notes",
                "code": (
                    'print(count(group=G0.where(Expression(Field("body", "analysis"), Value()) '
                    "!= None)))\n"
                ),
            },
        )
        assert _is_error(result)
        payload = _as_dict(result)
        assert payload["diagnostic"]["error_class"] == "QuailFieldError"
        assert "registered as source" in payload["diagnostic"]["message"]

    asyncio.run(run())


def test_exec_wrong_kind_field_commit_error_class(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        started = _as_dict(await _call(server, "quail_start_session"))
        result = await _call(
            server,
            "quail_exec",
            {
                "session_id": started["session_id"],
                "dataset_id": "notes",
                "code": 'bad = Field("body", "analysis")\nprint(bad.kind)\n',
            },
        )
        assert _is_error(result)
        payload = _as_dict(result)
        assert payload["diagnostic"]["error_class"] == "QuailFieldError"
        assert "registered as source" in payload["diagnostic"]["message"]

    asyncio.run(run())


def test_exec_scope_error_class(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        started = _as_dict(await _call(server, "quail_start_session"))
        result = await _call(
            server,
            "quail_exec",
            {
                "session_id": started["session_id"],
                "dataset_id": "notes",
                "code": 'print(retrieve(unit=Unit("fields"), group=G0, limit=1))\n',
            },
        )
        assert _is_error(result)
        payload = _as_dict(result)
        assert payload["diagnostic"]["error_class"] == "QuailScopeError"

    asyncio.run(run())


def test_exec_unknown_dataset_error_class(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        started = _as_dict(await _call(server, "quail_start_session"))
        result = await _call(
            server,
            "quail_exec",
            {
                "session_id": started["session_id"],
                "dataset_id": "no-such-dataset",
                "code": "print(1)\n",
            },
        )
        assert _is_error(result)
        payload = _as_dict(result)
        assert payload["diagnostic"]["error_class"] == "QuailScopeError"

    asyncio.run(run())


def test_exec_bad_time_window_error_class(tmp_path: Path) -> None:
    server, _, _ = _seed(tmp_path)

    async def run() -> None:
        started = _as_dict(await _call(server, "quail_start_session"))
        result = await _call(
            server,
            "quail_exec",
            {
                "session_id": started["session_id"],
                "dataset_id": "notes",
                "code": "print(1)\n",
                "time_window": "forever",
            },
        )
        assert _is_error(result)
        payload = _as_dict(result)
        assert payload["diagnostic"]["error_class"] == "QuailSyntaxError"

    asyncio.run(run())
