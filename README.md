# Quail v0.11

Inspectable qualitative analysis over private corpora (rebuild in progress).

This tree is intentionally small. Product decisions live in
[`docs/BOUNDARY.md`](docs/BOUNDARY.md). The model-facing analysis contract is
[`docs/api.md`](docs/api.md). The previous implementation at
`../Quail v0.10` is reference-only.

## Status

Analysis facade matches `docs/api.md` as symbolic AST.
Host planner + QueryEngine evaluate retrieve/count/tag against Turso + overlay
(`run_analysis` → `commit_overlay`); Lexical/Semantic and worker sandbox still
deferred. Datasets: CSV import into immutable versions. Sessions: revision-checked
overlay commit (host APIs, not MCP-facing).

## Intended operator path (not implemented)

```sh
quail run --config /absolute/path/to/quail.toml
```
