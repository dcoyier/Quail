"""Thin shell client over the official MCP Streamable HTTP transport."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

DEFAULT_URL = "http://127.0.0.1:8000/mcp"
# Cover Quail extended quail_exec wall time (100s) with headroom.
_READ_TIMEOUT_SECONDS = 300.0
_CONNECT_TIMEOUT_SECONDS = 30.0
_TOOL_READ_TIMEOUT = timedelta(seconds=120)


def main(argv: list[str] | None = None) -> int:
    """Parse argv, run list/call, print JSON to stdout. Give back an exit code."""

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as raised:
        code = raised.code
        return int(code) if isinstance(code, int) else 2

    try:
        if args.command == "list":
            payload = asyncio.run(list_tools(args.url))
            _print_json(payload)
            return 0
        if args.command == "call":
            arguments = load_arguments(args.arguments)
            result = asyncio.run(call_tool(args.url, args.tool_name, arguments))
            _print_json(_call_result_payload(result))
            return 1 if result.isError else 0
        parser.error(f"Unknown command: {args.command}")
        return 2
    except ValueError as error:
        print(f"mcp_client: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"mcp_client: {error}", file=sys.stderr)
        return 1


def load_arguments(spec: str) -> dict[str, Any]:
    """Load a JSON object from inline text, @path, or stdin (-)."""

    if spec == "-":
        raw = sys.stdin.read()
    elif spec.startswith("@"):
        path = Path(spec[1:]).expanduser()
        raw = path.read_text(encoding="utf-8")
    else:
        raw = spec
    try:
        value = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as error:
        raise ValueError(f"arguments must be a JSON object: {error}") from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("arguments must be a JSON object")
    return value


async def list_tools(url: str) -> dict[str, Any]:
    """Connect, initialize, list tools. Give back a JSON-ready mapping."""

    async with _session(url) as session:
        result = await session.list_tools()
    return {
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.inputSchema,
            }
            for tool in result.tools
        ]
    }


async def call_tool(
    url: str,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> CallToolResult:
    """Connect, initialize, call one tool. Give back the MCP CallToolResult."""

    async with _session(url) as session:
        return await session.call_tool(
            name,
            arguments or {},
            read_timeout_seconds=_TOOL_READ_TIMEOUT,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m quail.mcp_client",
        description=(
            "Generic MCP Streamable HTTP client (list tools / call a tool). "
            "Fallback for agents without a native MCP client."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List tools from an MCP server")
    list_parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"MCP endpoint URL (default: {DEFAULT_URL})",
    )

    call_parser = sub.add_parser("call", help="Call one MCP tool with JSON arguments")
    call_parser.add_argument("tool_name", help="Tool name to call")
    call_parser.add_argument(
        "arguments",
        help="JSON object, @path.json, or - for stdin",
    )
    call_parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"MCP endpoint URL (default: {DEFAULT_URL})",
    )
    return parser


@asynccontextmanager
async def _session(url: str) -> AsyncIterator[ClientSession]:
    """Open Streamable HTTP, initialize a ClientSession, then close cleanly."""

    async with AsyncExitStack() as stack:
        http_client = await stack.enter_async_context(
            httpx.AsyncClient(
                timeout=httpx.Timeout(
                    _CONNECT_TIMEOUT_SECONDS,
                    read=_READ_TIMEOUT_SECONDS,
                ),
                follow_redirects=True,
            )
        )
        transport = streamable_http_client(url, http_client=http_client)
        read_write = await stack.enter_async_context(transport)
        read_stream, write_stream = read_write[0], read_write[1]
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        yield session


def _call_result_payload(result: CallToolResult) -> dict[str, Any]:
    return {
        "isError": bool(result.isError),
        "structuredContent": result.structuredContent,
        "content": [block.model_dump(mode="json") for block in result.content],
    }


def _print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
