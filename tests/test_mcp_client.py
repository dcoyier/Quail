"""Shell MCP client: argument loading + Streamable HTTP list/call."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from quail.datasets import import_csv_dataset, open_core_db
from quail.mcp import create_mcp_server
from quail.mcp_client import load_arguments, main
from quail.mcp_client.mcp_client import call_tool, list_tools


def test_load_arguments_inline_file_and_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert load_arguments("{}") == {}
    assert load_arguments('{"a": 1}') == {"a": 1}

    path = tmp_path / "args.json"
    path.write_text('{"session_id": "s1"}', encoding="utf-8")
    assert load_arguments(f"@{path}") == {"session_id": "s1"}

    monkeypatch.setattr("sys.stdin", _FakeStdin('{"x": true}'))
    assert load_arguments("-") == {"x": True}

    with pytest.raises(ValueError, match="JSON object"):
        load_arguments("[1]")
    with pytest.raises(ValueError, match="JSON object"):
        load_arguments("not-json")


def test_main_rejects_bad_usage() -> None:
    assert main([]) == 2
    assert main(["call"]) == 2


def test_list_and_call_against_live_server(tmp_path: Path) -> None:
    url = _start_quail_mcp(tmp_path)

    listed = asyncio.run(list_tools(url))
    names = {tool["name"] for tool in listed["tools"]}
    assert "quail_setup" in names
    assert "quail_exec" in names

    setup = asyncio.run(call_tool(url, "quail_setup", {}))
    assert setup.isError is False
    assert setup.structuredContent is not None
    assert "session_id" in setup.structuredContent
    assert "datasets" in setup.structuredContent

    session_id = setup.structuredContent["session_id"]
    dataset_id = setup.structuredContent["datasets"][0]["dataset_id"]
    exec_args = {
        "session_id": session_id,
        "dataset_id": dataset_id,
        "code": "print(count())",
    }
    outcome = asyncio.run(call_tool(url, "quail_exec", exec_args))
    assert outcome.isError is False
    assert outcome.structuredContent is not None
    assert "printed_output" in outcome.structuredContent
    assert "2" in outcome.structuredContent["printed_output"]


def test_main_list_prints_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    url = _start_quail_mcp(tmp_path)
    code = main(["list", "--url", url])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(tool["name"] == "quail_setup" for tool in payload["tools"])


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


_SERVERS: list[uvicorn.Server] = []


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_quail_mcp(tmp_path: Path) -> str:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,title,body\ne1,Hello,hydrangea care tips\ne2,Other,climate notes\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "core.turso"
    feedback_path = tmp_path / "feedback.jsonl"
    with open_core_db(db_path) as db:
        import_csv_dataset(db, "local", "notes", csv_path, activate=True)

    mcp = create_mcp_server(db_path, feedback_path, workspace_id="local")
    app = mcp.streamable_http_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _SERVERS.append(server)

    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.time() + 10
    while time.time() < deadline:
        if server.started:
            return url
        time.sleep(0.05)
    raise RuntimeError("uvicorn MCP server failed to start")
