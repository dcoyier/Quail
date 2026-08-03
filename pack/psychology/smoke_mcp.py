#!/usr/bin/env python3
"""Smoke unrestricted MCP: quail_setup + quail_exec Semantic retrieve.

Requires: `quail run` serving streamable-http at --url (default
http://127.0.0.1:8000/mcp), Ollama embeddinggemma on :11434, and `mcp` installed
(shipped in quail-wheel/deps).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any


def _text_blobs(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _as_dict(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    raw = _text_blobs(result)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"tool result not JSON: {raw[:500]!r}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"tool result not an object: {type(payload)}")
    return payload


async def _run(url: str, query: str, limit: int) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(url) as (read, write, _get_sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("MCP_TOOLS", " ".join(names))
            for required in ("quail_setup", "quail_exec"):
                if required not in names:
                    print(f"SMOKE_MCP_FAIL missing tool {required}", file=sys.stderr)
                    return 1

            setup_result = await session.call_tool("quail_setup", {})
            if getattr(setup_result, "isError", False):
                print("SMOKE_MCP_FAIL quail_setup error", _text_blobs(setup_result), file=sys.stderr)
                return 1
            setup = _as_dict(setup_result)
            session_id = setup.get("session_id")
            datasets = setup.get("datasets") or []
            if not session_id or not isinstance(datasets, list) or not datasets:
                print("SMOKE_MCP_FAIL bad setup payload", setup, file=sys.stderr)
                return 1
            dataset_id = datasets[0].get("dataset_id")
            if not dataset_id:
                print("SMOKE_MCP_FAIL no dataset_id", datasets[0], file=sys.stderr)
                return 1
            print("MCP_SETUP_OK", f"session={session_id}", f"dataset={dataset_id}")

            code = f"""
query = {query!r}
score = Expression(Field("body"), Semantic(query))
matching = G0.where(score > 0)
rank = Ranking(expression=score)
hits = retrieve(group=matching, rank=rank, limit={limit})
vals = retrieve(unit=score, group=matching, rank=rank, limit={limit})
print("SEMANTIC")
for i in range(len(hits)):
    body = hits[i].value(Field("body")) or ""
    print(hits[i].id, round(vals[i], 4), body[:160].replace("\\n", " "))
lex_score = Expression(Field("body"), Lexical(query))
lex_matching = G0.where(lex_score > 0)
lex_rank = Ranking(expression=lex_score)
lex_hits = retrieve(group=lex_matching, rank=lex_rank, limit=3)
print("LEXICAL", len(lex_hits))
"""
            exec_result = await session.call_tool(
                "quail_exec",
                {
                    "session_id": session_id,
                    "dataset_id": dataset_id,
                    "code": code,
                    "time_window": "extended",
                },
            )
            if getattr(exec_result, "isError", False):
                print("SMOKE_MCP_FAIL quail_exec error", _text_blobs(exec_result), file=sys.stderr)
                return 1
            payload = _as_dict(exec_result)
            printed = payload.get("printed_output") or ""
            sys.stdout.write(printed)
            if not printed.endswith("\n"):
                sys.stdout.write("\n")
            if "SEMANTIC" not in printed:
                print("SMOKE_MCP_FAIL missing SEMANTIC in printed_output", file=sys.stderr)
                return 1
            print("SMOKE_MCP_OK")
            return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--url",
        default="http://127.0.0.1:8000/mcp",
        help="Streamable-HTTP MCP endpoint",
    )
    p.add_argument(
        "--query",
        default="cognitive dissonance and attitude change",
    )
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args()
    try:
        return asyncio.run(_run(args.url, args.query, args.limit))
    except Exception as exc:
        print(f"SMOKE_MCP_FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
