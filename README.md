# Quail v0.11

Inspectable qualitative analysis over private corpora (rebuild in progress).

This tree is intentionally small. Product decisions live in
[`docs/BOUNDARY.md`](docs/BOUNDARY.md). The model-facing analysis contract is
[`docs/api.md`](docs/api.md). The previous implementation at
`../Quail v0.10` is reference-only.

## Status

`quail run --config /absolute/path/to/quail.toml` loads a slim hand-edited
manifest, imports declared CSV datasets into the core DB, and serves
unrestricted loopback MCP. Feedback stays in a separate JSONL file.
Analysis facade, worker `exec_script`, sessions, and overlays are in place;
Lexical/Semantic and OIDC still deferred.

## Operator path

Edit `quail.toml` by hand (the CLI never writes it). Paths inside the file are
relative to the manifest directory. Restart to pick up changes:

```sh
# from a checkout, using the checked-in example (use a real absolute path)
uv run quail run --config /absolute/path/to/Quail/examples/quail.toml
```

See [`examples/quail.toml`](examples/quail.toml).
