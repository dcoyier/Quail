# Menus pack (dish grain)

NYPL *What's on the Menu?* dish-line corpus, prepared for the same
shard → parallel Ollama embed → parent warm pattern as psychology/biology.

## Build

```sh
# 1) Flatten the NYPL export (if not already done)
python examples/nypl-menus/prepare.py

# 2) Build data/dishes.csv + 20 unique-body shards
python pack/menus/prepare_dishes.py
```

`data/dishes.csv` is gitignored (~237MB). Shards under `pack/menus/shards/`
are committed (~2.6MB × 20).

## Embedding contract

Same as other domain packs:

- Field: `body` only (`fields = ["body"]` in root `quail.toml`)
- Body = stripped `dish_name` (leading/trailing whitespace removed)
- `text_hash` = SHA-256 of that exact UTF-8 string
- Shard rows: unique `{text_hash, body}`
- Model: `embeddinggemma:300m-qat-q8_0`, dimensions 768, revision `embeddinggemma-300m-qat-q8-v1`

## Embed workers

Each cloud agent owns **one** shard. See prompts from the parent agent.
Worker script: `pack/menus/embed_worker.py` (alias of `pack/tools/embed_worker.py`).
Output: `pack/menus/vectors/shard-XX.jsonl.gz` — commit **only** that file after
verifying gzip line count matches the shard.
