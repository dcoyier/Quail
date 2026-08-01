# Quail v0.11 Boundary

Familiarity-first rebuild. v0.10 at `../Quail v0.10` is a **reference and test oracle**, not a template.

## Purpose

Rebuild Quail so the author can understand and extend it. Keep load-bearing invariants; redesign anything hard to explain or ceremonial.

**Analysis contract:** [`docs/api.md`](api.md) is the model-facing API (facade types, `retrieve`/`tag`/…, regex, ranking, lexical/semantic). Grow code to match that document. The wheel force-includes it as `quail/data/api.md` so installed servers can serve it without the repo tree.

**Change routing:** [`docs/development.md`](development.md).

## Deployments

- **Local / research:** unrestricted loopback, no sign-in.
- **Soak / deliberate public tunnel:** unrestricted plus
  `hosting.allow_public_unrestricted = true` (anyone who can reach the URL can
  call MCP; no Clerk). Fail-closed without the override.
- **Company (~10 users):** HTTPS + Clerk/OIDC, users allowlisted in TOML.
  Non-loopback `http://` public origins require
  `hosting.allow_insecure_http = true` (fail-closed otherwise).

## Audiences

| Who | Interface |
| --- | --- |
| Agent | MCP + analysis language (`quail_exec`, print-only output) plus |
|  | `provide_feedback` for friction / improvement notes |
| Operator | Hand-edited `quail.toml` + `quail process` / `quail run` (CLI never writes TOML); |
|  | install connector wheels and pin/connect them in TOML |
| Connector author | Thin SDK (`quail.connectors.sdk`): tools, dataset docs, MCP UI widgets, narrow file routes |

### MCP agent tools (core)

Unrestricted loopback FastMCP (`create_mcp_server`) exposes:

| Tool | Role |
| --- | --- |
| `quail_setup` | Cold-start: analysis docs + dataset catalog + fresh session |
| `quail_get_api_docs` | Analysis language text from packaged `docs/api.md` |
| `quail_list_datasets` | Workspace dataset catalog |
| `quail_start_session` | Create an active session |
| `quail_get_dataset_info` | Name/id plus guidance (`dataset_document` from connectors when present) |
| `quail_exec` | Worker `exec_script` for one session + dataset |
| `provide_feedback` | Append friction/improvement notes to a JSONL file |

Prefer `quail_setup` at connect (and after workspace switch) over separately
calling docs/list/start. Dataset-specific guidance still comes from
`quail_get_dataset_info` unless `hosting.include_dataset_docs_in_setup = true`
embeds those docs in the setup payload.

Connected connectors may add further tools, resources, MCP UI widgets, and
narrow GET file routes for the active workspace.

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
layer (path configured beside the core DB or via constructor args). Each
message is capped at 16 KiB (UTF-8); appends fail closed once the file reaches
64 MiB.

Connect-time agent orientation lives in MCP **server instructions** plus
**tool descriptions** (not in `docs/api.md`). Dataset-specific guidance is
fetched via `quail_get_dataset_info`, not embedded in static instructions.
- Users are **not** nested in per-workspace files.
- One user record with `workspaces = ["acme", "labs"]`.
- One MCP session binds to **one** workspace at a time.
- Unrestricted mode omits `[[users]]`.

## Operator commands

```sh
quail process --config /absolute/path/to/quail.toml
quail run --config /absolute/path/to/quail.toml
```

`process` and `run` take an exclusive deployment lease (`fcntl.flock` lock files
beside `core.database` and, when set, `core.search_database`, acquired in sorted
path order). Local POSIX filesystems only; NFS lock semantics vary. If the lease
is held, `process` fails fast (“stop the server, then retry”).

`process` reads slim TOML → import CSVs **without** activating → warm Lexical
FTS and (when declared) unbounded corpus embeddings for those imported versions
→ **publish** (activate all successful imports + add/update/delete embedding
pins). Warm-all-then-activate-all: a later dataset failure leaves prior actives
unchanged. Knobs live under `[search.warm]` (`embed_batch_size`,
`max_concurrent_embed_requests`). `process --clear` wipes search warm/vectors/FTS
for the versions being warmed, then re-warms; core data is never deleted.

Same-version in-place rewarm (config-only / schema bump on the already-active
version) still rebuilds search artifacts in place; a mid-rewarm failure leaves
the deployment fail-closed until a successful re-process — never silently wrong.
Full shadow-build staging is deferred.

`run` holds the lease for the server lifetime, imports CSVs **without**
activating, fail-closes unless each imported version is already active **and**
its warm receipt matches the current TOML, then serves MCP (unrestricted
loopback, unrestricted with `allow_public_unrestricted`, or Clerk allowlist on
one URL). It never flips activation. On exit it closes the search connection
pool and disconnects connectors (when they implement `disconnect`), then
releases the lease. Edit TOML → process → run. The CLI never writes the TOML.

Unrestricted mode rejects non-loopback `hosting.bind` and non-loopback
`hosting.public_base_url` unless `hosting.allow_public_unrestricted = true`.
Loopback hosts are `127.0.0.1`, `localhost`, and `::1`. A Cloudflare (or other)
public origin fails closed even when bind is loopback.

`[hosting] max_concurrent_executions` (default `2`, range `1`–`100`) caps
simultaneous `quail_exec` slots for the whole process. Independent of
`[search.warm]` embed concurrency. Restart to apply.

### MCP concurrency (host)

- Blocking MCP tool bodies run via `anyio.to_thread` so the asyncio loop stays
  responsive while Turso/file/network work runs.
- Each tool invocation opens its own **CoreDb** connection (no process-wide
  shared core handle on unrestricted).
- Each in-flight `quail_exec` checks out its own **SearchDb** from a pool sized
  to `max_concurrent_executions` (Lexical + Semantic for that exec share only
  that handle).
- `time_window` wall/memory limits set a cancel event, kill the analysis worker,
  and best-effort `connection.interrupt()` on that exec’s core/search
  connections. Mid-query interrupt is not hard real-time. Residual exposure
  inside a single `dispatch_call` is non-DB host compute only; cancel is checked
  before and after each dispatch, not mid-call.
- Overlapping `quail_exec` on the same `session_id` fails fast with
  `session_busy` (enforced in-process lock).

Clerk mode is **identity + allowlist**, not full OAuth resource-server scope
enforcement. A Clerk token proves `sub`; `auth.clerk_authorized_parties` must
match JWT `azp` or `aud` (application binding). TOML `[[users]]` is the tool
gate. MCP still advertises openid/profile/email for client compatibility;
Quail does not read or enforce those scopes from the token. JWT-shaped tokens
never fall back to userinfo after signature, issuer, time, claim, or party
failure. Only opaque (non-JWT-shaped) tokens use Clerk userinfo and skip the
azp/aud claim check (issuer still vouches). Sessions store `owner_user_id`
(TOML user id); only that user may exec or attach feedback to the session.

Clerk mode also serves MCP OAuth discovery so clients can sign in via Clerk:
protected-resource metadata for `{public_base_url}/mcp` and a proxied
`/.well-known/oauth-authorization-server`. Optional
`hosting.public_base_url` defaults from `bind`/`port`. Operators allowlist users
by pasting Clerk `user_…` ids into `[[users]]` and set
`auth.clerk_authorized_parties` to the Clerk application party id(s).

### Connectors (operator)

- Install the connector wheel into Quail’s environment (`uv pip install …`).
- Pin exact `id` + `version` under deployment-wide `[[extensions]]`.
- Activate per workspace with `[[workspaces.connectors]]` (config + dataset
  bindings). No TOML path to source; discovery uses Python entry points.
- `quail run` **fail-closes** on missing/mismatched pins, construct/connect
  errors, or two+ connectors providing dataset docs for the same bound
  `dataset_id` in one workspace (tools may still overlap).

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
- Search **infrastructure** extras: ANN and lexical workers/artifact roots
- Hosting flourishes (ngrok as product; `public_base_url` remains)
- Connector **events** host API (file GET routes are in; pub/sub events are not)

Semantic uses per-dataset embedding profiles (Ollama/OpenRouter), a rebuildable
Turso vector cache, and **exact** batch cosine via `-vector_distance_dot`.
**OpenRouter privacy:** when a dataset pins `provider = "openrouter"`, warm and
query embedding export send the full field text for each embedded segment to
OpenRouter’s remote API (off-host). Prefer Ollama for corpora that must stay
local. Lexical uses Turso **native FTS** in the same rebuildable search DB
(in-process): one warmed corpus table pair per source field, plus request-local
scratch corpora for derived/analysis/unwarmed scoring. Both Lexical and Semantic
accept `str`, `list[str]`, entry `GroupExpr`, and `list[Entry]` query targets.
Session bindings persist across successful execs.

## Build order (stop if you cannot explain the step)

1. This boundary + empty layout — **done**
2. Analysis contract [`api.md`](api.md) + facade/namespace — **done**
3. Immutable dataset import + read — **done**
4. Session overlays + revision commit — **done**
5. Planner + engine (plus Lexical/Semantic and worker) — **done**
6. Worker + print-only + RPC — **done**
7. Thin MCP adapter — **done**
8. TOML + `quail process` / `quail run` — **done**
9. Clerk + TOML allowlist + OAuth discovery — **done** (invitations / generic OIDC still out)
10. Connector SDK — **done:** tools, dataset docs (`dataset_document` →
    `quail_get_dataset_info`), MCP UI widgets, narrow GET file routes
    (`RouteSpec` / `FileResponse`); see [`connector-sdk.md`](connector-sdk.md)

## Working agreement

- Prefer re-expression over file copy from v0.10.
- When unsure: preserve the invariant, change the API/shape.
- Use v0.10 tests as an oracle for worker/commit behavior.
