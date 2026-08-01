# Quail Development

Architecture and change routing for Quail v0.11 from an existing checkout.
Product decisions live in [`BOUNDARY.md`](BOUNDARY.md). The model-facing
analysis contract is [`api.md`](api.md). Connector authors read
[`connector-sdk.md`](connector-sdk.md).

## Package map

| Area | Owns |
| --- | --- |
| `quail/analysis/` | Facade, planner, engine, worker, bindings, ranking, Lexical/Semantic wiring |
| `quail/datasets/` | Core Turso, CSV import, catalog reads |
| `quail/session/` | Sessions, overlays, revision commit |
| `quail/search/` | Search Turso, FTS, vectors, warm, pool |
| `quail/mcp/` | MCP tools, offload, feedback JSONL, Clerk sticky workspace, OAuth discovery |
| `quail/config/` | Slim hand-edited TOML models and parse |
| `quail/auth/` | Clerk JWT verification |
| `quail/run/` | `process` / `run` / apply / warm gate |
| `quail/connectors/` | Connector host loader (private) and public `quail.connectors.sdk` |
| `quail/providers/` | Embedding HTTP providers |

## Change routing

| Change | Where to look |
| --- | --- |
| Analysis language / agent `quail_exec` contract | `docs/api.md` then `quail/analysis/` |
| Deploy, auth mode, concurrency, warm | `docs/BOUNDARY.md` then `quail/config/`, `quail/run/`, `quail/mcp/` |
| Connector author surface | `docs/connector-sdk.md` then `quail.connectors.sdk` only |
| Tests as oracle | Prefer v0.11 tests; v0.10 at `../Quail v0.10` is reference only |

## Module layout

Python modules under `quail/` use a folder pair (`.py` + `.txt` twin). See
`AGENTS.md`. Tests under `tests/` are plain `.py` files only.

## Frozen host surface

Agents use the core MCP tools:
`quail_setup`, `quail_list_datasets`, `quail_start_session`,
`quail_get_dataset_info`, `search`, `get_entry`, `provide_feedback`, plus Clerk
workspace tools when `auth.mode = "clerk"`. Connected connectors may add
tools, resources, and MCP UI widgets.

**Experimental (this branch):** `search` / `get_entry` substitute for
`quail_exec` / `quail_get_api_docs` on the agent surface. Analysis stack remains
in-tree for the canned hybrid recipe. See `examples/rag-baseline/README.md`.
Soak regression path: `examples/quail.soak.toml` (local soak DBs stay untracked).
