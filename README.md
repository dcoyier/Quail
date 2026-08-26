# Quail v0.11

Quail is an MCP environment for qualitative analysis of a text corpus. Agents
write bounded Python against a compact analysis API. They do not chat over
retrieved chunks.

Researchers hold text worth deciding from, and not enough time to read it.
Chat and retrieval hide the operations that connect evidence to claims, and
they cannot examine a corpus past what fits in context.

Analysis happens inside `quail_exec`. You build inert descriptions, evaluate
them with `retrieve` / `count` / `tag` / `untag`, and `print` what should leave
the sandbox. Those names are not MCP tools. `Lexical` and `Semantic` are score
ops in that language, not a chat retriever. A successful exec commits prints,
tags, and bindings together; a failure keeps nothing.

If you are connected: call `quail_setup` once. It returns `documentation` (the
analysis language), a dataset catalog, and `session_id`. Pick a `dataset_id`.
Then `quail_exec(session_id, dataset_id, code)`. Reuse that `session_id`
serially. Field names are not in the catalog — print them from exec. If
`quail_list_workspaces` is in the tool list (Clerk), bind a workspace, then
setup; do not reuse an old `session_id`.

If you are bringing the server up: [Run it](#run-it).

- Analyze: [How to analyze](#how-to-analyze)
- Local vs persistent: [Local or remote](#local-or-remote)
- Bring the server up: [Run it](#run-it)
- Shared host: [Clerk](#clerk)
- Language: [`docs/api.md`](docs/api.md) · Semantics: [`docs/core.md`](docs/core.md)

## How to analyze

`quail_setup` once at cold start. Prefer it over `quail_get_api_docs`,
`quail_list_datasets`, and `quail_start_session`.

1. Pick `dataset_id` from setup’s `datasets`. `quail_get_dataset_info` is
   corpus notes, not a field schema (skip if setup already inlined those docs).
2. `quail_exec(session_id, dataset_id, code)`. Pass arguments by name.
   Success is `{"printed_output": "..."}` — only `print()` leaves the sandbox.
   Failure is a diagnostic; nothing partial is kept. Optional `time_window`:
   `"standard"` (30s wall / 15s CPU) or `"extended"` (100s / 60s). Worker RSS
   256 MiB. Hitting a ceiling fails the whole exec.
3. Reuse the same `session_id` serially. One in-flight `quail_exec` per session
   (`session_busy` if you overlap, including with `quail_export_csv`). Do not
   use another user’s `session_id`. After `quail_switch_workspace`, setup (or
   `quail_start_session`) again.

`provide_feedback` is for friction and improvements, not analysis results.
`quail_export_csv` writes a host filesystem path on the machine running
`quail run`, not a download — [Export and privacy](#export-and-privacy).

Core tools: `quail_setup`, `quail_get_api_docs`, `quail_list_datasets`,
`quail_start_session`, `quail_get_dataset_info`, `quail_exec`,
`quail_export_csv`, `provide_feedback`. Clerk adds `quail_list_workspaces` and
`quail_switch_workspace`. Connectors may add tools, resources, and MCP UI
widgets for the active workspace.

### What is true

Canonical copy: [`docs/core.md`](docs/core.md). If this list and that file
disagree, that file wins.

1. **A dataset is an immutable grid.** Entries × fields, JSON-like values.
   Absence is `None`. Imported source data never changes.
2. **A session adds an overlay.** Analysis fields and tags live on the
   session, scoped to one dataset version. Source fields cannot be created or
   overwritten. Bindings are session-scoped names restored on the next exec.
3. **The language builds inert descriptions.** An `Expression` pipes one
   field's value per entry through typed ops. A `Predicate` is a boolean per
   entry. A `GroupExpr` is a set of entries or fields, closed under `& | ~`.
   A `Ranking` is a non-negative linear combination of numeric expressions.
   A `Unit` picks what comes back. Construction never reads data.
4. **Evaluation happens only at four verbs.** `retrieve`, `count`, `tag`,
   `untag` (`entry.value` and `entry.fields` read through the same engine).
5. **An exec is a transaction.** Prints, tags, and bindings commit together or
   not at all. Later lines see earlier tags; failure rolls everything back.
6. **Search is not special.** `Lexical` and `Semantic` are ordinary ops that
   produce a score. Warm paths are optimizations, never semantics.

No imports; names are injected. Omitted `retrieve` `limit` defaults to **1**.
Do not use Python `and` / `or` / `not` on Predicates; use `== None`, not
`is None`. Overlay writes are `create_field` / `tag` / `untag` — see
[`docs/api.md`](docs/api.md).

### First exec

Field names differ per dataset. `G0` is all entries; `G1` is all fields;
`fields` / `entries` are units (not groups). The CSV `id` column is `entry.id`,
not `Field("id")`. Empty cells are `None`, not `""`.

```python
for field in retrieve(unit=fields, group=G1, limit=50):
    print(field.name, field.kind)

samples = retrieve(limit=1)
if len(samples) > 0:
    for field in samples[0].fields():
        print(field.name, repr(samples[0].value(field)))
```

Setup’s `documentation` is the rest of [`docs/api.md`](docs/api.md). From here,
follow the question.

## Local or remote

Same analysis language. Two ways to instantiate the server.

- **Local (unrestricted).** One fixed workspace, no sign-in, loopback by
  default ([`examples/quail.toml`](examples/quail.toml)). A one-off server on
  that machine. Fits a throwaway VM: run the task, discard the machine.
- **Remote (Clerk).** One URL, Clerk identity, TOML `[[users]]` allowlist,
  persistent workspaces, shared datasets
  ([`examples/quail.clerk.toml`](examples/quail.clerk.toml)). Sessions belong
  to the creating user. Setup is [Clerk](#clerk).

Unauthenticated non-loopback MCP (including a wildcard bind) requires
`hosting.allow_public_unrestricted = true` (fail-closed without it). Clerk
public origins should be `https://` unless you set
`hosting.allow_insecure_http = true`.

## Run it

Requires Python 3.12–3.13. Hand-edit `quail.toml`. The CLI never writes it.
`--config` must be absolute. Paths inside the TOML are relative to the
manifest directory. Unknown keys fail closed. Template comments in
[`examples/quail.toml`](examples/quail.toml) are the rest of the manifest.

```sh
uv sync
```

1. Copy [`examples/quail.toml`](examples/quail.toml) (or point `--config` at a
   copy).
2. Next to the copied TOML, create `data/notes.csv` (UTF-8, unique `id`
   column). That path is relative to the TOML, not the repo root, and is not
   in the tree. Keep the `[[datasets]]` `id` stable when the CSV changes.
3. Process, then run:

```sh
uv run quail process --config /absolute/path/to/quail.toml
uv run quail run --config /absolute/path/to/quail.toml
```

4. Connect an MCP client to `http://127.0.0.1:8000/mcp` (default bind/port).

### process then run

- **`process`** — imports declared CSVs, warms Lexical FTS and any corpus
  embeddings when a search database is configured, then activates those
  versions. Pins embeddings when a dataset declares them. With no search
  database, import and activate only.
- **`run`** — takes a deployment lease, imports without activating, fail-closes
  unless each imported version is already active (and, when a search database
  is set, warm receipts match the TOML), then serves MCP. Never activates.

`process` and `run` cannot hold the same deployment lease at once. Stop
`quail run` before `quail process`. If the lease is held, the CLI prints the
error and a repair hint (`Stop the running Quail server…`).

Re-run `process` after changing the embedding profile (`provider`, `model`,
`dimensions`, `revision`, or `fields`) or `[datasets.lexical]`. Keep the same
dataset `id` when the CSV changes: embeddings for unchanged text are copied
from any prior version of that `id` with the same embedding profile; only new
strings are embedded. A new `id` rebuilds from scratch. Changing the embedding
profile also rebuilds. `[search.warm]` batch/concurrency apply on the next
`process`; they do not by themselves require a rebuild. `quail process --clear`
requires a search database: it wipes search artifacts for versions being warmed
and rebuilds them without copying; core CSV data is untouched.

`[hosting] max_concurrent_executions` (default `2`) caps simultaneous
`quail_exec` work process-wide and sizes the search pool. Restart `quail run`
after changing it.

### Export and privacy

`quail_export_csv` writes source columns plus this session's tags to a CSV on
the machine running `quail run` (a host `path`, not a download) so those tag
columns can be processed as **source**. That `quail process` step is the
warm-path speedup (`Lexical` / `Semantic` skip cell load). Export itself does
not reprocess. Remote clients can call the tool; the path and process stay on
the host.

If a dataset pins `provider = "openrouter"`, warm and query embedding exports
send full field text off-host. Prefer Ollama when text must stay local.

### Shell fallback

If you do not have a native MCP client, use the thin Streamable HTTP helper.
Default URL is `http://127.0.0.1:8000/mcp`. `--url` may sit before or after the
subcommand (`list --url …` still works). Arguments are a JSON object,
`@path.json`, or `-` (stdin). On `list` / `call`, stdout is JSON only.

```sh
uv run python -m quail.mcp_client list
uv run python -m quail.mcp_client --url http://127.0.0.1:8000/mcp list
uv run python -m quail.mcp_client call quail_setup '{}'
uv run python -m quail.mcp_client call quail_exec @exec.json
```

## Clerk

Clerk proves identity (`sub`); `auth.clerk_authorized_parties` binds tokens to
your Clerk app (`azp`/`aud`); TOML `[[users]]` gates MCP access (not only
tool bodies). Advertised OAuth scopes are for client UX — Quail does not
enforce them from the token. Sessions belong to the creating user.

Template: [`examples/quail.clerk.toml`](examples/quail.clerk.toml). Bind a
workspace before dataset, session, or exec tools. Omit `default_workspace` to
stay unbound until `quail_switch_workspace`. `lock_workspace` pins that user
to their default — list/switch will not change it.

1. In Clerk, enable **Dynamic client registration** and default scopes
   `openid`, `profile`, `email` (MCP client UX).
2. Put the application party id in `auth.clerk_authorized_parties`.
3. Invite users in Clerk. Each `[[users]]` row needs a local `id`, the Clerk
   `user_…` id as `clerk_user_id`, and workspace memberships. Optional
   `default_workspace`; `lock_workspace` requires it.
4. Set `hosting.public_base_url` to the origin clients use (no `/mcp` path;
   defaults to `http://{bind}:{port}`). Required when bind is `0.0.0.0` or
   `::`. Expose that origin and `/mcp` (proxy or ngrok).
5. `quail process` then `quail run`, and add `{public_base_url}/mcp` in Cursor
   or Claude. Sign-in is Clerk; Quail still enforces the TOML allowlist.

## Docs

| File | Role |
| --- | --- |
| [`docs/api.md`](docs/api.md) | Model-facing analysis language (`quail_exec`) |
| [`docs/core.md`](docs/core.md) | Load-bearing semantics |
| [`docs/development.md`](docs/development.md) | Change routing from an existing checkout |
| [`docs/connector-sdk.md`](docs/connector-sdk.md) | Trusted connector author surface |

## Background

Quail started from work with the [Carleton College DataSquad](https://arxiv.org/abs/2511.19688),
a student-staffed data and software support group: campus offices collected
surveys with rich long-answer fields and lacked time for deeper review. The
composition idea follows Cloudflare’s [Code Mode](https://blog.cloudflare.com/code-mode/):
tools inside code, not a chain of discrete calls.

<p align="center">
  <img src="quail.png" alt="Quail Logo">
</p>

Apache-2.0 · Dashiell Coyier · Python 3.12–3.13 · [uv](https://docs.astral.sh/uv/)
