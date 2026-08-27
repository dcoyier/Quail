# Quail Development

Architecture and change routing for Quail v0.11 from an existing checkout.
Core semantics live in [`core.md`](core.md). The model-facing analysis
contract is [`api.md`](api.md). Connector authors read
[`connector-sdk.md`](connector-sdk.md). Operator how-to:
[`README.md`](../README.md).

## Package map

| Area | Owns |
| --- | --- |
| `quail/analysis/` | Facade, planner, engine, worker, bindings, ranking, Lexical/Semantic wiring |
| `quail/datasets/` | Core Turso, CSV import, catalog reads |
| `quail/session/` | Sessions, overlays, revision commit |
| `quail/search/` | Search Turso, FTS, vectors, warm, pool |
| `quail/mcp/` | MCP tools, offload, feedback JSONL, Clerk sticky workspace, OAuth discovery |
| `quail/mcp_client/` | Thin Streamable HTTP shell client (`python -m quail.mcp_client`) when no native MCP client is available |
| `quail/config/` | Slim hand-edited TOML models and parse |
| `quail/auth/` | Clerk JWT verification |
| `quail/run/` | `process` / `run` / apply / warm gate |
| `quail/connectors/` | Connector host loader (private) and public `quail.connectors.sdk` |
| `quail/providers/` | Embedding HTTP providers |

## Change routing

| Change | Where to look |
| --- | --- |
| Analysis semantics / op pipeline rules | `docs/core.md`, then `OP_SPECS` in `quail/analysis/operations/` |
| Analysis language / agent `quail_exec` contract | `docs/api.md` then `quail/analysis/` |
| Deploy, auth mode, concurrency, warm | `README.md`, then `quail/config/`, `quail/run/`, `quail/mcp/` |
| Shell fallback MCP client | `README.md` Quick start, then `quail/mcp_client/` |
| Connector author surface | `docs/connector-sdk.md` then `quail.connectors.sdk` only |
| Tests | Prefer v0.11 tests under `tests/` |

## Module layout

Python modules under `quail/` use a folder pair (`.py` + `.txt` twin). See
`AGENTS.md`. Tests under `tests/` are plain `.py` files only.

## Frozen host surface

Agents use the core MCP tools:
`quail_setup`, `quail_get_api_docs`, `quail_list_datasets`, `quail_start_session`,
`quail_get_dataset_info`, `quail_exec`, `quail_export_csv`, `provide_feedback`,
plus Clerk workspace tools when `auth.mode = "clerk"`. Connected connectors may add
tools, resources, and MCP UI widgets; connector tools are listed only for
the active workspace. Connect to
`http://127.0.0.1:8000/mcp` with MCP 2026-07-28 (no initialize handshake).
If you do not have a native MCP client, use
`uv run python -m quail.mcp_client` (`list` / `call`) against that URL.
