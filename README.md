# Quail v0.11

Inspectable qualitative analysis over private corpora (rebuild in progress).

This tree is intentionally small. Product decisions live in
[`docs/BOUNDARY.md`](docs/BOUNDARY.md). The model-facing analysis contract is
[`docs/api.md`](docs/api.md). The previous implementation at
`../Quail v0.10` is reference-only.

## Status

`quail run --config /absolute/path/to/quail.toml` loads a slim hand-edited
manifest, imports declared CSV datasets, and serves MCP.

- **Unrestricted:** fixed workspace, loopback, no sign-in
  ([`examples/quail.toml`](examples/quail.toml)).
- **Clerk:** Bearer JWT + TOML `[[users]]` allowlist on one deployment URL,
  sticky workspace via `quail_list_workspaces` / `quail_switch_workspace`
  ([`examples/quail.clerk.toml`](examples/quail.clerk.toml)).

Feedback stays in a separate JSONL file. Semantic similarity uses a per-dataset
embedding profile (`[datasets.embedding]` plus `[providers.*]` and
`core.search_database`) with Turso exact batch cosine (not ANN). Lexical FTS
and invitations are still deferred.

## Operator path

Edit `quail.toml` by hand (the CLI never writes it). Paths inside the file are
relative to the manifest directory. Restart to pick up changes:

```sh
uv run quail run --config /absolute/path/to/Quail/examples/quail.toml
```
