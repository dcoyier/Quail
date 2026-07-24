# Quail v0.11

Inspectable qualitative analysis over private corpora (rebuild in progress).

This tree is intentionally small. Product decisions live in
[`docs/BOUNDARY.md`](docs/BOUNDARY.md). The model-facing analysis contract is
[`docs/api.md`](docs/api.md). The previous implementation at
`../Quail v0.10` is reference-only.

## Status

`quail process --config /absolute/path/to/quail.toml` imports declared CSVs and
warms Lexical FTS plus corpus embeddings into the search database.
`quail run --config …` applies CSVs, fail-closes unless warm receipts match the
current TOML pins, then serves MCP.

- **Unrestricted:** fixed workspace, loopback, no sign-in
  ([`examples/quail.toml`](examples/quail.toml)).
- **Clerk:** Bearer JWT + TOML `[[users]]` allowlist on one deployment URL,
  sticky workspace via `quail_list_workspaces` / `quail_switch_workspace`
  ([`examples/quail.clerk.toml`](examples/quail.clerk.toml)).

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

After changing `[datasets.embedding]` or `[search.warm]`, run `quail process`
again. Use `quail process --clear` to wipe search artifacts for active versions
and rebuild under the current TOML (core CSV versions stay untouched).
