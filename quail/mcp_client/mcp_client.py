"""Thin shell client over the official MCP Streamable HTTP transport."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp.client import Client
from mcp.types import CallToolResult

DEFAULT_URL = "http://127.0.0.1:8000/mcp"
PROTOCOL_VERSION = "2026-07-28"
# Cover Quail extended quail_exec wall time (100s) with queue/headroom budget.
_READ_TIMEOUT_SECONDS = 300.0


def main(argv: list[str] | None = None) -> int:
    """Parse argv, run list/call, print JSON to stdout. Give back an exit code."""

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as raised:
        code = raised.code
        return int(code) if isinstance(code, int) else 2

    try:
        url = _mcp_url(args)
        if args.command == "list":
            payload = asyncio.run(list_tools(url))
            _print_json(payload)
            return 0
        try:
            arguments = load_arguments(args.arguments)
        except ValueError as error:
            print(f"mcp_client: {error}", file=sys.stderr)
            return 2
        result = asyncio.run(call_tool(url, args.tool_name, arguments))
        _print_json(_call_result_payload(result))
        return 1 if result.is_error else 0
    except Exception as error:
        print(f"mcp_client: {_client_error_message(error)}", file=sys.stderr)
        return 1


def load_arguments(spec: str) -> dict[str, Any]:
    """Load a JSON object from inline text, @path, or stdin (-)."""

    if spec == "-":
        raw = sys.stdin.read()
    elif spec.startswith("@"):
        path = Path(spec[1:]).expanduser()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"could not read arguments file: {error}") from error
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
    """Connect, list tools. Give back a JSON-ready mapping."""

    async with _client(url) as client:
        result = await client.list_tools()
    return {
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in result.tools
        ]
    }


async def call_tool(
    url: str,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> CallToolResult:
    """Connect, call one tool. Give back the MCP CallToolResult."""

    async with _client(url) as client:
        return await client.call_tool(
            name,
            arguments or {},
            read_timeout_seconds=_READ_TIMEOUT_SECONDS,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m quail.mcp_client",
        description=(
            "Generic MCP Streamable HTTP client (list tools / call a tool). "
            "Use when you do not have a native MCP client."
        ),
    )
    parser.add_argument(
        "--url",
        dest="url_before",
        default=None,
        help=f"MCP endpoint URL (default: {DEFAULT_URL})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List tools from an MCP server")
    list_parser.add_argument(
        "--url",
        dest="url_after",
        default=None,
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
        dest="url_after",
        default=None,
        help=f"MCP endpoint URL (default: {DEFAULT_URL})",
    )
    return parser


def _mcp_url(args: argparse.Namespace) -> str:
    """Prefer --url after the subcommand, then before it, then DEFAULT_URL."""

    after = getattr(args, "url_after", None)
    before = getattr(args, "url_before", None)
    return after or before or DEFAULT_URL


def _client(url: str) -> Client:
    """MCP 2026-07-28 Streamable HTTP client."""

    return Client(
        url,
        read_timeout_seconds=_READ_TIMEOUT_SECONDS,
        mode=PROTOCOL_VERSION,
    )


def _client_error_message(error: BaseException) -> str:
    """Unwrap TaskGroup/ExceptionGroup so connection failures are readable."""

    current: BaseException = error
    while isinstance(current, BaseExceptionGroup) and current.exceptions:
        current = current.exceptions[0]
    text = str(current).strip()
    if (not text) or "TaskGroup" in text or "unhandled errors" in text.lower():
        nested = current.__cause__ or current.__context__
        if nested is not None:
            nested_text = str(nested).strip()
            if nested_text:
                return nested_text
    return text or type(current).__name__


def _call_result_payload(result: CallToolResult) -> dict[str, Any]:
    return {
        "isError": bool(result.is_error),
        "structuredContent": result.structured_content,
        "content": [
            block.model_dump(mode="json", by_alias=True) for block in result.content
        ],
    }


def _print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
