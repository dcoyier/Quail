# Quail v0.11

Inspectable qualitative analysis over private corpora.

This tree stays small. Product decisions live in
[`docs/BOUNDARY.md`](docs/BOUNDARY.md). The model-facing analysis contract is
[`docs/api.md`](docs/api.md). Change routing is
[`docs/development.md`](docs/development.md). The previous implementation at
`../Quail v0.10` is reference-only.

## Status

First-build spine through Clerk, search warm, `quail_exec` concurrency, and the
Connector SDK (extra tools, dataset docs, MCP UI widgets) is in place. Author
guide: [`docs/connector-sdk.md`](docs/connector-sdk.md). Example package:
[`examples/garden-gate-connector/`](examples/garden-gate-connector/).

`quail process --config /absolute/path/to/quail.toml` imports declared CSVs and
warms Lexical FTS plus corpus embeddings into the search database.
`quail run --config …` applies CSVs, fail-closes unless warm receipts match the
current TOML pins, then serves MCP.

- **Unrestricted:** fixed workspace, loopback, no sign-in
  ([`examples/quail.toml`](examples/quail.toml)).
- **Clerk:** Bearer JWT / OAuth access tokens + TOML `[[users]]` allowlist on one
  deployment URL, sticky workspace via `quail_list_workspaces` /
  `quail_switch_workspace` ([`examples/quail.clerk.toml`](examples/quail.clerk.toml)).
  MCP OAuth discovery (`/.well-known/oauth-protected-resource/mcp` plus an
  authorization-server metadata proxy) points clients at Clerk; operators paste
  `user_…` ids into TOML. Set `hosting.public_base_url` when the public origin
  differs from `bind`/`port` (e.g. ngrok).

Feedback stays in a separate JSONL file. Semantic similarity uses a per-dataset
embedding profile (`[datasets.embedding]` plus `[providers.*]` and
`core.search_database`) with Turso exact batch cosine (not ANN). Lexical FTS
uses Turso native FTS in the same rebuildable search database (in-process).
Both accept `str`, `list[str]`, entry groups, and Entry lists as query targets.
Invitations remain deferred.

## Operator path

Edit `quail.toml` by hand (the CLI never writes it). Paths inside the file are
relative to the manifest directory. Process once per version/embedding profile,
then run (and re-run) without re-embedding:

```sh
uv run quail process --config /absolute/path/to/Quail/examples/quail.toml
uv run quail run --config /absolute/path/to/Quail/examples/quail.toml
```

After changing `[datasets.embedding]` (including `fields`) or `[search.warm]`,
run `quail process` again. Use `quail process --clear` to wipe search artifacts
for active versions and rebuild under the current TOML (core CSV versions stay
untouched).

`[hosting] max_concurrent_executions` (default `2`) caps simultaneous
`quail_exec` work process-wide; it is independent of `[search.warm]` embed
concurrency. Restart `quail run` after changing it.

### Clerk / MCP clients

1. In the Clerk Dashboard, enable **Dynamic client registration** under OAuth
   applications and set default scopes to `openid`, `profile`, and `email`.
2. Invite people in Clerk; copy each `user_…` id into `[[users]]` in
   `quail.toml` with workspace memberships.
3. Set `hosting.public_base_url` to the origin clients will use (defaults to
   `http://{bind}:{port}`). Expose that origin (and `/mcp`) via reverse proxy or
   ngrok as needed.
4. `quail process` then `quail run`, and add `{public_base_url}/mcp` in Cursor /
   Claude. The client should open Clerk sign-in; Quail still enforces the TOML
   allowlist after token verification.
