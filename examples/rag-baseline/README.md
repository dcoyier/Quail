# RAG baseline (experimental branch surface)

This Quail branch swaps the agent work tool from `quail_exec` to opaque hybrid
retrieval:

- `search(session_id, dataset_id, query, top_k=8)` — Lexical + Semantic via a
  canned analysis recipe, then host RRF
- `get_entry(session_id, dataset_id, entry_id)` — full source row

Analysis / `quail_exec` implementation stays in-tree but is **not** registered.
`docs/api.md` is not served in `quail_setup`.

## Hardcoded search field

`quail.mcp.rag_baseline.constants.SEARCH_FIELD` is `"body"` (matches
`examples/data/notes.csv`). Change that constant to target another column.

## Run

Use a normal Quail TOML with `core.search_database` and
`[datasets.embedding]` so both Lexical and Semantic arms can run, then:

```sh
quail process --config /absolute/path/to/quail.toml
quail run --config /absolute/path/to/quail.toml
```

Agent workflow: `quail_setup` → `quail_get_dataset_info` → `search` → optional
`get_entry` → answer in chat.
