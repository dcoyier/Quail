# Quail v0.11

Inspectable qualitative analysis over private corpora.

Inspired by OpenAI's Code Interpreter, agents compose analysis inside
restricted Python rather than one-off tool calls. Quail exposes a compact
Python API for filtering, counting, inspecting, and tagging entries; objects
combine and nest to set scope, filters, and ranking. Successful work persists
in session variables and dataset annotations; only printed output returns from
the tool call. Retrieval sits inside that programmable environment, not as the
only capability. Quail ships as an MCP server with local and remote modes.

Hand-edit `quail.toml`, import CSVs with `quail process`, then serve with
`quail run`. The CLI never writes the TOML.

- Analysis language: [`docs/api.md`](docs/api.md)
- Change routing: [`docs/development.md`](docs/development.md)
- Connector SDK: [`docs/connector-sdk.md`](docs/connector-sdk.md) ·
  [`examples/notes-connector/`](examples/notes-connector/)

## Quick start

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

### process vs run

- **`process`** — imports declared CSVs, warms Lexical FTS and any corpus
  embeddings, then activates those versions and embedding pins.
- **`run`** — takes a deployment lease, imports without activating, fail-closes
  unless each imported version is already active and warm receipts match the
  TOML, then serves MCP. Never activates.

Re-run `process` after changing `[datasets.embedding]` (including `fields`) or
`[search.warm]`. `quail process --clear` wipes search artifacts for versions
being warmed and rebuilds them; core CSV data is untouched.

`[hosting] max_concurrent_executions` (default `2`) caps simultaneous
`quail_exec` work process-wide. Restart `quail run` after changing it.

### Modes

- **Unrestricted** — fixed workspace, loopback, no sign-in
  ([`examples/quail.toml`](examples/quail.toml)).
- **Clerk** — JWT/OAuth identity + TOML `[[users]]` allowlist on one URL
  ([`examples/quail.clerk.toml`](examples/quail.clerk.toml)).

Public unrestricted tunnel: set `hosting.allow_public_unrestricted = true`
(fail-closed without it). Clerk public origins should be `https://` unless you
set `hosting.allow_insecure_http = true`.

**Privacy:** if a dataset pins `provider = "openrouter"`, warm and query
embedding exports send full field text off-host. Prefer Ollama when text must
stay local.

## Clerk setup

Clerk proves identity (`sub`); `auth.clerk_authorized_parties` binds tokens to
your Clerk app (`azp`/`aud`); TOML `[[users]]` gates tools. Advertised OAuth
scopes are for client UX — Quail does not enforce them from the token. Sessions
belong to the creating user.

1. In Clerk, enable **Dynamic client registration** and default scopes
   `openid`, `profile`, `email`.
2. Put the application party id in `auth.clerk_authorized_parties`.
3. Invite users in Clerk; paste each `user_…` id into `[[users]]` with
   workspace memberships.
4. Set `hosting.public_base_url` to the origin clients use (defaults to
   `http://{bind}:{port}`). Expose that origin and `/mcp` (proxy or ngrok).
5. `quail process` then `quail run`, and add `{public_base_url}/mcp` in Cursor
   or Claude. Sign-in is Clerk; Quail still enforces the TOML allowlist.
