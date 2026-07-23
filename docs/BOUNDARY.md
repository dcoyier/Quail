# Quail v0.11 Boundary

Familiarity-first rebuild. v0.10 at `../Quail v0.10` is a **reference and test oracle**, not a template.

## Purpose

Rebuild Quail so the author can understand and extend it. Keep load-bearing invariants; redesign anything hard to explain or ceremonial.

**Analysis contract:** [`docs/api.md`](api.md) is the model-facing API (facade types, `retrieve`/`tag`/…, regex, ranking, lexical/semantic). Grow code to match that document.

## Deployments

- **Local / research:** unrestricted loopback, no sign-in.
- **Company (~10 users):** HTTP + Clerk/OIDC, users allowlisted in TOML.

## Audiences

| Who | Interface |
| --- | --- |
| Agent | MCP + analysis language (`quail_exec`, print-only output) plus |
|  | `provide_feedback` for friction / improvement notes |
| Operator | Hand-edited `quail.toml` + `quail run --config …` (CLI never writes TOML) |
| Connector author | Deferred — not in the first build |

### MCP agent tools (first slice)

Unrestricted loopback FastMCP (`create_mcp_server`) exposes only:

| Tool | Role |
| --- | --- |
| `quail_get_api_docs` | Analysis language text from `docs/api.md` |
| `quail_list_datasets` | Workspace dataset catalog |
| `quail_start_session` | Create an active session |
| `quail_get_dataset_info` | Name/id plus short guidance |
| `quail_exec` | Worker `exec_script` for one session + dataset |
| `provide_feedback` | Append friction/improvement notes to a JSONL file |

`provide_feedback(message, *, category=None, session_id=None, dataset_id=None)`
is for confusing, blocked, or improvable Quail behavior — not analysis results.
The bar is low: expected outcomes that did not occur, or anything that would
have improved the experience. Agents learn this tool from MCP, not from
`docs/api.md`.

## Deployment model

One process, one TOML, one authoritative **core** DB for workspaces, datasets,
sessions, and overlays:

```text
Deployment
├── workspace(s)
│   └── dataset(s)   # many datasets in one DB, not one DB per dataset
└── users            # deployment-wide; memberships list workspaces
```

Agent `provide_feedback` notes are **not** stored in the core analysis DB.
They go to a separate append-only feedback JSONL file owned by the MCP/host
layer (path configured beside the core DB or via constructor args).

Connect-time agent orientation lives in MCP **server instructions** plus
**tool descriptions** (not in `docs/api.md`). Dataset-specific guidance is
fetched via `quail_get_dataset_info`, not embedded in static instructions.
- Users are **not** nested in per-workspace files.
- One user record with `workspaces = ["acme", "labs"]`.
- One MCP session binds to **one** workspace at a time.
- Unrestricted mode omits `[[users]]`.

## Operator command

```sh
quail run --config /absolute/path/to/quail.toml
```

Read TOML → apply declared state → serve. Refresh = edit file, stop, run again.

## Preserve (invariants — improve shape, do not weaken)

- Immutable imported dataset versions
- Session-scoped analysis overlays
- One dataset version per `exec`
- Print-only caller-visible output
- Host never execs user code; worker has no DB
- Analysis language as an explicit API (not arbitrary Python-on-DB)

## Improve

- Small modules; no god-object service pulling the whole deployment graph
- Hand-edited TOML only (no console, no CLI config writers, no invite product)
- Core importable/testable without MCP

## Out of first build

- Operator console
- validate/doctor/plan/apply ceremony
- Invitations / identity linking / live admin user APIs
- Connector author SDK
- Search **infrastructure** (indexes/embeddings) may land after the API/AST surface; Lexical/Semantic stay in `api.md`
- Hosting flourishes (ngrok, etc.)

## Build order (stop if you cannot explain the step)

1. This boundary + empty layout
2. Analysis contract [`api.md`](api.md) + facade/namespace grown to match it
3. Immutable dataset import + read — first slice: embedded Turso + UTF-8 CSV
   (no TOML reconcile yet)
4. Session overlays + revision commit — first slice: host `commit_overlay`
   with optimistic revision (no MCP/worker yet)
5. Planner + engine — first slice: host `plan_*` + QueryEngine + `run_analysis`
   (Lexical/Semantic and worker sandbox still deferred)
6. Worker + print-only + RPC — first slice: subprocess `exec_script` + NDJSON
   ApiCalls into `dispatch_call` (bindings persist and Lexical still deferred)
7. Thin MCP adapter — **first slice done:** unrestricted loopback FastMCP with
   `quail_get_api_docs`, `quail_list_datasets`, `quail_start_session`,
   `quail_get_dataset_info`, `quail_exec`, and `provide_feedback` (JSONL store
   separate from the core analysis DB). TOML / `quail run` still next.
8. TOML + `quail run --config` (loopback)
9. OIDC/Clerk + TOML allowlist
10. Connector SDK (later)

Search **infrastructure** may follow the AST/API surface; Lexical/Semantic remain part of the public contract in `api.md`.

## Working agreement

- Prefer re-expression over file copy from v0.10.
- When unsure: preserve the invariant, change the API/shape.
- Use v0.10 tests as an oracle for worker/commit behavior.
