"""Shell MCP client: argument loading + Streamable HTTP list/call."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from mcp.types import CallToolResult, TextContent

from quail.datasets import import_csv_dataset, open_core_db
from quail.mcp import create_mcp_server
from quail.mcp_client import load_arguments, main
from quail.mcp_client.mcp_client import _stdout_packet, call_tool, list_tools


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


def test_stdout_packet_prefers_structured_content() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text='{"b": 2}')],
        structured_content={"a": 1},
        is_error=False,
    )
    assert _stdout_packet(result) == {"a": 1}


def test_stdout_packet_error_is_diagnostic_object() -> None:
    payload = {
        "execution_id": None,
        "diagnostic": {"error_class": "QuailScopeError", "message": "missing"},
    }
    result = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
        is_error=True,
    )
    assert _stdout_packet(result) == payload


def test_stdout_packet_unstructured_error_becomes_diagnostic() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="Unknown tool: nope")],
        structured_content=None,
        is_error=True,
    )
    assert _stdout_packet(result) == {
        "execution_id": None,
        "diagnostic": {
            "error_class": "QuailSyntaxError",
            "stable_error_code": "quail_syntax_error",
            "message": "Unknown tool: nope",
        },
    }


def test_main_rejects_bad_usage() -> None:
    assert main([]) == 2
    assert main(["call"]) == 2


def test_main_call_bad_json_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["call", "quail_setup", "not-json"])
    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "mcp_client:" in captured.err


def test_main_call_missing_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing.json"
    code = main(["call", "quail_setup", f"@{missing}"])
    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "mcp_client:" in captured.err


def test_main_call_connection_failure_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "call",
            "quail_setup",
            "{}",
            "--url",
            "http://127.0.0.1:1/mcp",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "mcp_client:" in captured.err
    assert "TaskGroup" not in captured.err


def test_main_url_before_subcommand_connects_to_that_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    async def _capture(url: str, *_args: object, **_kwargs: object) -> None:
        seen.append(url)
        raise ConnectionError("refused")

    monkeypatch.setattr("quail.mcp_client.mcp_client.call_tool", _capture)
    code = main(
        [
            "--url",
            "http://127.0.0.1:1/mcp",
            "call",
            "quail_setup",
            "{}",
        ]
    )
    assert code == 1
    assert seen == ["http://127.0.0.1:1/mcp"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "mcp_client:" in captured.err
    assert "invalid choice" not in captured.err
    assert "refused" in captured.err


def test_client_error_message_unwraps_exception_group() -> None:
    from quail.mcp_client.mcp_client import _client_error_message

    inner = ConnectionError("All connection attempts failed")
    grouped = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner])
    assert _client_error_message(grouped) == "All connection attempts failed"


def test_main_call_sdk_value_error_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _raise(*_args: object, **_kwargs: object) -> None:
        raise ValueError("unexpected content type")

    monkeypatch.setattr("quail.mcp_client.mcp_client.call_tool", _raise)
    code = main(["call", "quail_setup", "{}"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "mcp_client:" in captured.err
    assert "unexpected content type" in captured.err


def test_auto_client_speaks_2026(mcp_url: str) -> None:
    from mcp.client import Client

    from quail.mcp_client.mcp_client import PROTOCOL_VERSION

    async def _auto() -> str:
        async with Client(mcp_url, mode="auto") as client:
            tools = await client.list_tools()
            assert any(tool.name == "quail_setup" for tool in tools.tools)
            return client.protocol_version

    assert asyncio.run(_auto()) == PROTOCOL_VERSION


def test_legacy_client_can_list_tools(mcp_url: str) -> None:
    from mcp.client import Client

    async def _legacy() -> str:
        async with Client(mcp_url, mode="legacy") as client:
            tools = await client.list_tools()
            assert any(tool.name == "quail_setup" for tool in tools.tools)
            return client.protocol_version

    assert asyncio.run(_legacy()) == "2025-11-25"


def test_handshake_initialize_is_accepted(tmp_path: Path) -> None:
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp_types import UNSUPPORTED_PROTOCOL_VERSION
    from starlette.testclient import TestClient

    server = create_mcp_server(tmp_path / "core.turso", tmp_path / "feedback.jsonl")
    app = server.streamable_http_app(
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "quail-test", "version": "0"},
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body.get("error", {}).get("code") != UNSUPPORTED_PROTOCOL_VERSION
    assert "result" in body
    assert body["result"]["protocolVersion"]


def test_list_and_call_against_live_server(mcp_url: str) -> None:
    listed = asyncio.run(list_tools(mcp_url))
    names = {tool["name"] for tool in listed["tools"]}
    assert "quail_setup" in names
    assert "quail_exec" in names

    setup = asyncio.run(call_tool(mcp_url, "quail_setup", {}))
    assert setup.is_error is False
    assert setup.structured_content is not None
    assert "session_id" in setup.structured_content
    assert "datasets" in setup.structured_content

    session_id = setup.structured_content["session_id"]
    dataset_id = setup.structured_content["datasets"][0]["dataset_id"]
    exec_args = {
        "session_id": session_id,
        "dataset_id": dataset_id,
        "code": "print(count())",
    }
    outcome = asyncio.run(call_tool(mcp_url, "quail_exec", exec_args))
    assert outcome.is_error is False
    assert outcome.structured_content is not None
    assert "printed_output" in outcome.structured_content
    assert "2" in outcome.structured_content["printed_output"]


def test_main_list_and_call_exit_codes(mcp_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list", "--url", mcp_url]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert any(tool["name"] == "quail_setup" for tool in listed["tools"])

    assert main(["call", "quail_setup", "{}", "--url", mcp_url]) == 0
    setup = json.loads(capsys.readouterr().out)
    assert setup["session_id"]
    assert "structuredContent" not in setup
    assert "isError" not in setup
    assert "content" not in setup

    assert (
        main(
            [
                "call",
                "quail_exec",
                json.dumps(
                    {
                        "session_id": setup["session_id"],
                        "dataset_id": "notes",
                        "code": "print(count())",
                    }
                ),
                "--url",
                mcp_url,
            ]
        )
        == 0
    )
    outcome = json.loads(capsys.readouterr().out)
    assert "2" in outcome["printed_output"]
    assert "structuredContent" not in outcome

    assert (
        main(
            [
                "call",
                "quail_exec",
                json.dumps(
                    {
                        "session_id": setup["session_id"],
                        "dataset_id": "missing-dataset",
                        "code": "print(1)",
                    }
                ),
                "--url",
                mcp_url,
            ]
        )
        == 1
    )
    failed = json.loads(capsys.readouterr().out)
    assert "diagnostic" in failed
    assert "isError" not in failed

    assert main(["call", "quail_exec", "{}", "--url", mcp_url]) == 1
    missing_args = json.loads(capsys.readouterr().out)
    assert missing_args["diagnostic"]["stable_error_code"] == "quail_syntax_error"
    assert "session_id" in missing_args["diagnostic"]["message"]


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


@pytest.fixture
def mcp_url(tmp_path: Path) -> Iterator[str]:
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

    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.time() + 10
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("uvicorn MCP server failed to start")

    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
