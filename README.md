# Quail v0.11

Inspectable qualitative analysis over private corpora (rebuild in progress).

This tree is intentionally small. Product decisions live in
[`docs/BOUNDARY.md`](docs/BOUNDARY.md). The model-facing analysis contract is
[`docs/api.md`](docs/api.md). The previous implementation at
`../Quail v0.10` is reference-only.

## Status

Analysis facade matches `docs/api.md` as symbolic AST (evaluation still stubbed).
Datasets: embedded Turso + UTF-8 CSV import into immutable versions, with source
field/entry/value reads (no TOML apply yet).
Sessions: host overlay commit with optimistic `state_revision` (create_field/tag
persist only via `commit_overlay`; not MCP-facing).

## Intended operator path (not implemented)

```sh
quail run --config /absolute/path/to/quail.toml
```
