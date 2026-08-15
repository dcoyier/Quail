<p align="center">
  <img src="quail.png" alt="Quail Logo">
</p>

# Quail v0.11

## About

At the center of Quail is an analysis language inspired by Cloudflare’s “Code
Mode”, an approach that lets agents compose tool calls inside code rather than
making discrete requests. Quail adapts this general idea into an environment
built for qualitative analysis, exposing a symbolic analysis language through a
compact Python API. Within this environment, the agent writes blocks of
restricted Python that call the analysis API against a processed dataset, using
core operations for retrieving, counting, inspecting, and tagging entries. The
flexibility of these operations comes from interdependent classes whose objects
can be combined and nested to define scope, filtering, and ranking. Successful
turns preserve variables and dataset annotations for later analysis, and only
what is printed during the Python execution is passed as the result of the tool
call. In a way, this environment extends retrieval-augmented generation;
retrieval remains a way for the agent to be grounded in evidence, but it is
part of a programmable analysis environment instead of the sole capability.
Quail is packaged as a harness-agnostic MCP server with explicit local and
remote server modes.

-> [Read more (old version)](https://drive.google.com/file/d/1J7vOg3eojIIS6DqAOCjG6Qov0dOKl9zy/view?usp=sharing)

## Quick start

Hand-edit `quail.toml`, import CSVs with `quail process`, then serve with
`quail run`. The CLI never writes the TOML.

- Analysis language: [`docs/api.md`](docs/api.md)
- Change routing: [`docs/development.md`](docs/development.md)
- Connector SDK: [`docs/connector-sdk.md`](docs/connector-sdk.md) ·
  [`examples/notes-connector/`](examples/notes-connector/)

```sh
uv sync
```

1. Copy [`examples/quail.toml`](examples/quail.toml) (or point `--config` at it).
2. Add a CSV and a `[[datasets]]` row. Paths in the TOML are relative to the
   manifest directory.
3. Process, then run. `--config` must be absolute:

```sh
uv run quail process --config /absolute/path/to/quail.toml
uv run quail run --config /absolute/path/to/quail.toml
```

4. Connect an MCP client to `http://127.0.0.1:8000/mcp` (default bind/port).
   If you do not have a native MCP client, use the thin Streamable HTTP helper:

```sh
uv run python -m quail.mcp_client list
uv run python -m quail.mcp_client call quail_setup '{}'
uv run python -m quail.mcp_client call quail_exec @exec.json
```

   Default URL is `http://127.0.0.1:8000/mcp`. Pass `--url` to override.
   Arguments are a JSON object, `@path.json`, or `-` (stdin). stdout is JSON
   only.

### process vs run

- **`process`** — imports declared CSVs, warms Lexical FTS and any corpus
  embeddings, then activates those versions and embedding pins.
- **`run`** — takes a deployment lease, imports without activating, fail-closes
  unless each imported version is already active and warm receipts match the
  TOML, then serves MCP. Never activates.

`process` and `run` cannot hold the same deployment lease at once. Stop
`quail run` before `quail process`. If the lease is held, the CLI prints the
error and a repair hint (`Stop the running Quail server…`).

Re-run `process` after changing `[datasets.embedding]` (including `fields`) or
`[search.warm]`. Keep the same dataset `id` when the CSV changes: embeddings
for unchanged text are copied from any prior version of that `id` with the same
embedding profile; only new strings are embedded. A new `id` rebuilds from
scratch. Changing the embedding profile (provider, model, dimensions, revision,
or `fields`) also rebuilds. `quail process --clear` wipes search artifacts for
versions being warmed and rebuilds them without copying; core CSV data is
untouched.

`[hosting] max_concurrent_executions` (default `2`) caps simultaneous
`quail_exec` work process-wide. Restart `quail run` after changing it.

## Modes

- **Unrestricted** — fixed workspace, no sign-in (loopback by default)
  ([`examples/quail.toml`](examples/quail.toml)).
- **Clerk** — JWT/OAuth identity + TOML `[[users]]` allowlist on one URL
  ([`examples/quail.clerk.toml`](examples/quail.clerk.toml)).

Public unrestricted tunnel: set `hosting.allow_public_unrestricted = true`
(fail-closed without it). Clerk public origins should be `https://` unless you
set `hosting.allow_insecure_http = true`.

`quail_export_csv` writes source columns plus this session's tags to a CSV on
the machine running `quail run` (a host `path`, not a download) so those tag
columns can be processed as **source**. That `quail process` step is the
warm-path speedup (`Lexical` / `Semantic` skip cell load). Export itself does
not reprocess. Remote clients can call the tool; the path and process stay on
the host.

**Privacy:** if a dataset pins `provider = "openrouter"`, warm and query
embedding exports send full field text off-host. Prefer Ollama when text must
stay local.

## Clerk setup

Clerk proves identity (`sub`); `auth.clerk_authorized_parties` binds tokens to
your Clerk app (`azp`/`aud`); TOML `[[users]]` gates MCP access (not only
tool bodies). Advertised OAuth scopes are for client UX — Quail does not
enforce them from the token. Sessions belong to the creating user.

1. In Clerk, enable **Dynamic client registration** and default scopes
   `openid`, `profile`, `email`.
2. Put the application party id in `auth.clerk_authorized_parties`.
3. Invite users in Clerk; paste each `user_…` id into `[[users]]` with
   workspace memberships.
4. Set `hosting.public_base_url` to the origin clients use (defaults to
   `http://{bind}:{port}`). Expose that origin and `/mcp` (proxy or ngrok).
5. `quail process` then `quail run`, and add `{public_base_url}/mcp` in Cursor
   or Claude. Sign-in is Clerk; Quail still enforces the TOML allowlist.
