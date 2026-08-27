<p align="center">
  <img src="quail.png" alt="Quail">
</p>

# Quail v0.11

Quail is an environment where agents explore a text corpus. Typically that
means surveys, notes, transcripts, and other long-form collections: text
that is worth deciding from, and too large to read exhaustively.

Language models make it easy to start asking questions of that kind of data.
On their own, they still struggle to examine a collection systematically,
and to keep the path from evidence to a conclusion in view. Conventional
chat and retrieval hide the operations that connect a claim to the rows
behind it, and both struggle once the corpus no longer fits in context.
Quail is built around those operations rather than around a retriever.

The design follows Cloudflare’s [Code Mode](https://blog.cloudflare.com/code-mode/).
Instead of a chain of discrete tool calls, the agent writes blocks of
restricted Python against a compact analysis API. Inside those blocks you
compose filters, scores, groups, and rankings as inert descriptions: they
do not read the corpus until you evaluate them by retrieving, counting,
inspecting, and tagging entries. Only printed output leaves the sandbox as
the result of the call. A successful turn keeps session variables and
dataset annotations for later work. A failed turn keeps nothing, so the
session you reuse is either fully updated or untouched.

Retrieval still grounds the agent in the corpus; it is one capability
inside the analysis language, not a chat retriever sitting in front of the
data.

Quail ships as a harness-agnostic MCP server. You can run it locally as a
one-off process, which fits a temporary VM: stand the agent and the server
up for the life of a task, then discard the machine. You can also run it
remotely with Clerk authentication, so several agents share a persistent
dataset.

If you are already connected to a running server, start with
[Working with a corpus](#working-with-a-corpus). If you are bringing a server
up yourself, skip ahead to [Running Quail](#running-quail). The analysis
language is specified in [`docs/api.md`](docs/api.md); the load-bearing
semantics are in [`docs/core.md`](docs/core.md).

## Working with a corpus

When you connect, call `quail_setup` once. It returns the three things you
need for the rest of the session: the analysis-language documentation, the
dataset catalog, and a fresh `session_id`. Prefer that single call over
requesting `quail_get_api_docs`, `quail_list_datasets`, and
`quail_start_session` separately.

On a Clerk deployment, `quail_list_workspaces` appears in the tool list.
Bind a workspace before setup, and do not reuse a `session_id` from a
previous workspace. Unrestricted local servers have one fixed workspace,
so you can go straight to setup.

From there the loop is small:

1. Pick a `dataset_id` from setup’s `datasets`. Field names are not in the
   catalog — print them from exec, using the snippet under
   [First exec](#first-exec).
2. Call `quail_exec(session_id, dataset_id, code)`, passing arguments by
   name. Success is `{"printed_output": "..."}`: only `print()` leaves the
   sandbox. Failure is a diagnostic, and nothing partial is kept — no tags,
   no bindings, no printed text.
3. Reuse that `session_id` serially. Bindings and tags persist across
   successful execs on the same session.

`quail_get_dataset_info` is optional corpus notes, not a field schema, so
skip it when setup already inlined those docs. The operator can enable
inlining with `include_dataset_docs_in_setup`. The names `retrieve`,
`count`, `tag`, and `untag` live inside the Python you pass to
`quail_exec`; they are not MCP tools.

Each exec is bounded. Pass an optional `time_window` of `"standard"` (30s
wall / 15s CPU) or `"extended"` (100s wall / 60s CPU). Worker RSS is capped
at 256 MiB either way, and hitting any ceiling fails the whole exec.

Only one `quail_exec` may be in flight per session, so overlap — including
with `quail_export_csv` — returns `session_busy`. Do not use another user’s
`session_id`. After `quail_switch_workspace`, call setup (or
`quail_start_session`) again rather than reusing the old session.

Use `provide_feedback` for friction and improvements, not for analysis
results. `quail_export_csv` writes a filesystem path on the machine running
`quail run`, not a file download; details are under
[Export and privacy](#export-and-privacy).

The frozen host tools are `quail_setup`, `quail_get_api_docs`,
`quail_list_datasets`, `quail_start_session`, `quail_get_dataset_info`,
`quail_exec`, `quail_export_csv`, and `provide_feedback`. Clerk adds
`quail_list_workspaces` and `quail_switch_workspace`. Connectors may add
tools, resources, and MCP UI widgets for the active workspace.

## The analysis model

These six statements are the spec. The canonical copy is
[`docs/core.md`](docs/core.md); if this list and that file disagree, that
file wins.

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

The sandbox injects the names you need, so there are no imports. Overlay
writes go through `create_field`, `tag`, and `untag`. An omitted `retrieve`
`limit` defaults to **1**, not the whole group.

Predicates compose with `&`, `|`, and `~` rather than Python `and` / `or` /
`not`, and absence is `== None`, not `is None`. The rest of the language —
operations, ranking, Lexical query syntax, the bounded Python surface — is
in [`docs/api.md`](docs/api.md), which setup also returns as `documentation`.

### First exec

Field names differ per dataset, so print them before assuming a schema.
`G0` is all entries and `G1` is all fields — `fields` and `entries` are
units, not groups. The CSV `id` column is `entry.id`, not `Field("id")`,
and empty cells are `None`, not `""`.

```python
for field in retrieve(unit=fields, group=G1, limit=50):
    print(field.name, field.kind)

samples = retrieve(limit=1)
if len(samples) > 0:
    for field in samples[0].fields():
        print(field.name, repr(samples[0].value(field)))
```

From here, follow the question. Setup’s `documentation` is the rest of the
language.

## Local and remote

The analysis language does not change with the deployment. What changes is
how long the server lives, and who can reach it.

**Local (unrestricted)** is one fixed workspace, no sign-in, loopback by
default. The template is [`examples/quail.toml`](examples/quail.toml). This
is a one-off server on that machine: the right default for a single agent
doing a bounded piece of analysis, including on a throwaway VM that exists
only for the task.

**Remote (Clerk)** is one URL, Clerk identity, a TOML `[[users]]` allowlist,
and persistent workspaces that several agents can share. The template is
[`examples/quail.clerk.toml`](examples/quail.clerk.toml). Sessions belong to
the creating user. Standing that up is [Clerk](#clerk).

Unauthenticated MCP on a non-loopback address — including a wildcard bind —
requires `hosting.allow_public_unrestricted = true`. Without that flag, the
server fail-closes. Clerk public origins should be `https://` unless you
set `hosting.allow_insecure_http = true`.

## Running Quail

Standing a server up is three steps: hand-edit a `quail.toml`, import a CSV
with `quail process`, and serve MCP with `quail run`. The CLI never writes
the TOML. `--config` must be an absolute path; paths inside the file are
relative to the manifest directory, and unknown keys fail closed. Quail
requires Python 3.12–3.13. Comments in
[`examples/quail.toml`](examples/quail.toml) document the rest of the
manifest.

```sh
uv sync
```

1. Copy [`examples/quail.toml`](examples/quail.toml), or point `--config` at
   your own copy.
2. Next to that TOML, create `data/notes.csv` (UTF-8, with a unique `id`
   column). The example `source` path is relative to the TOML, not the repo
   root, and that CSV is not in the tree. Keep the `[[datasets]]` `id`
   stable when the file changes, so later processing can reuse embeddings
   for unchanged text.
3. Process, then run:

```sh
uv run quail process --config /absolute/path/to/quail.toml
uv run quail run --config /absolute/path/to/quail.toml
```

4. Connect an MCP 2026-07-28 client to `http://127.0.0.1:8000/mcp` (the
   default bind/port).

### process then run

`process` publishes versions; `run` serves them. They cannot hold the same
deployment lease at once, so stop `quail run` before you `quail process`.
If the lease is held, the CLI prints the error and a repair hint
(`Stop the running Quail server…`).

- **`process`** imports the declared CSVs, warms Lexical FTS and any corpus
  embeddings when a search database is configured, then activates those
  versions. It pins embeddings when a dataset declares them. With no search
  database, it imports and activates only.
- **`run`** takes a deployment lease, imports without activating, and
  fail-closes unless each imported version is already active (and, when a
  search database is set, warm receipts match the TOML). Then it serves MCP.
  It never activates.

Re-run `process` after changing the embedding profile (`provider`, `model`,
`dimensions`, `revision`, or `fields`) or `[datasets.lexical]`. Keep the same
dataset `id` when the CSV changes: embeddings for unchanged text are copied
from any prior version of that `id` with the same embedding profile, and
only new strings are embedded. A new `id` rebuilds from scratch, as does a
change to the embedding profile.

`[search.warm]` batch and concurrency settings apply on the next `process`;
they do not by themselves require a rebuild. `quail process --clear` needs
a search database: it wipes search artifacts for the versions being warmed
and rebuilds them without copying. Core CSV data is untouched.

`[hosting] max_concurrent_executions` (default `2`) caps simultaneous
`quail_exec` work process-wide and sizes the search pool, so restart
`quail run` after changing it.

### Export and privacy

`quail_export_csv` writes source columns plus this session’s tags to a CSV
on the machine running `quail run`. The tool result is a host `path`, not
the file body, so an operator can process those tag columns as **source**.
That later `quail process` step is the warm-path speedup: `Lexical` and
`Semantic` can skip loading cells. Export itself does not reprocess. Remote
clients can call the tool, but the path and the process stay on the host.

If a dataset pins `provider = "openrouter"`, warm and query embedding
exports send full field text off-host. Prefer Ollama when the text must
stay local.

### Shell fallback

If you do not have a native MCP client, a thin Streamable HTTP helper is
included. The default URL is `http://127.0.0.1:8000/mcp`, and `--url` may
sit before or after the subcommand (`list --url …` still works). Arguments
are a JSON object, `@path.json`, or `-` for stdin. `call` stdout is the
tool result JSON object, not the MCP envelope (`list` prints
`{"tools": [...]}`). A tool error still prints that packet and exits 1.

```sh
uv run python -m quail.mcp_client list
uv run python -m quail.mcp_client --url http://127.0.0.1:8000/mcp list
uv run python -m quail.mcp_client call quail_setup '{}'
uv run python -m quail.mcp_client call quail_exec @exec.json
```

Setup’s stdout has `session_id` and `datasets` at the top level. Copy those
into `exec.json` with the analysis `code`:

```json
{"session_id": "ses_…", "dataset_id": "notes", "code": "print(count())"}
```

## Clerk

Remote deployments use Clerk to prove who is calling (`sub`), then a TOML
`[[users]]` allowlist to decide whether that person may use MCP at all —
not only whether individual tools succeed. Bind tokens to your Clerk
application with `auth.clerk_authorized_parties` (`azp` / `aud`). Advertised
OAuth scopes are for client UX; Quail does not enforce them from the token,
and sessions belong to the creating user. Sticky workspace is per Clerk
user for this process, not per MCP transport session:
`quail_switch_workspace` applies to every connection as that user.

The template is [`examples/quail.clerk.toml`](examples/quail.clerk.toml).
Bind a workspace before you call dataset, session, or exec tools. If you
omit `default_workspace`, the user stays unbound until
`quail_switch_workspace`. `lock_workspace` pins that user to their default,
and list/switch will not change it.

To stand up a shared host:

1. In Clerk, enable **Dynamic client registration** and the default scopes
   `openid`, `profile`, and `email` (MCP client UX).
2. Put the application party id in `auth.clerk_authorized_parties`.
3. Invite users in Clerk. Each `[[users]]` row needs a local `id`, the Clerk
   `user_…` id as `clerk_user_id`, and workspace memberships.
   `default_workspace` is optional; `lock_workspace` requires it.
4. Set `hosting.public_base_url` to the origin clients use (no `/mcp` path;
   it defaults to `http://{bind}:{port}`). This is required when bind is
   `0.0.0.0` or `::`. Expose that origin and `/mcp` (proxy or ngrok).
5. Run `quail process`, then `quail run`, and add `{public_base_url}/mcp` in
   Cursor or Claude. Sign-in is Clerk; Quail still enforces the TOML
   allowlist.

## Docs

| File | Role |
| --- | --- |
| [`docs/api.md`](docs/api.md) | Model-facing analysis language (`quail_exec`) |
| [`docs/core.md`](docs/core.md) | Load-bearing semantics |
| [`docs/development.md`](docs/development.md) | Change routing from an existing checkout |
| [`docs/connector-sdk.md`](docs/connector-sdk.md) | Trusted connector author surface |

## Background

Quail started from work with the [Carleton College DataSquad](https://arxiv.org/abs/2511.19688),
a student-staffed data and software support group. Campus offices were
collecting surveys with rich long-answer fields and reviewing them only
superficially, because they lacked the time and infrastructure for deeper
analysis. The immediate setting was local; the pattern is general. The
composition idea — tools inside code, not a chain of discrete calls —
follows Cloudflare’s [Code Mode](https://blog.cloudflare.com/code-mode/).

Apache-2.0 · Dashiell Coyier · Python 3.12–3.13 · [uv](https://docs.astral.sh/uv/)
