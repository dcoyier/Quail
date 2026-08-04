# Menus pack (dish + menu-document grain)

NYPL *What's on the Menu?* corpora at dish-line and full-menu grain, prepared
for the same shard → parallel Ollama embed → parent warm pattern as
psychology/biology.

## Build

```sh
# 1) Flatten the NYPL export (if not already done)
python examples/nypl-menus/prepare.py

# 2) Build data/dishes.csv + 20 unique dish-body shards
python pack/menus/prepare_dishes.py

# 3) Build data/menu_docs.csv + 20 unique menu-body shards
python pack/menu_docs/prepare_menu_docs.py
```

The generated CSVs are gitignored. Dish shards live under `pack/menus/shards/`;
full-menu shards live under `pack/menu_docs/shards/`.

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

## Warm and seal

Warm both corpora in one bounded run by repeating `--vectors-dir`. The combined
cache defaults to the first vector directory and can be reused after it has
been built from both directories:

```sh
python pack/tools/warm_precomputed.py \
  --config "$(pwd)/quail.toml" \
  --vectors-dir "$(pwd)/pack/menus/vectors" \
  --vectors-dir "$(pwd)/pack/menu_docs/vectors"
```

The successful combined build contains:

- `dishes`: 1,329,638 Lexical `body` texts and 410,795 unique vectors;
- `menu_docs`: 17,514 Lexical `body` texts and 17,501 unique vectors.

Both database WAL files must be empty afterward.

Split the warmed core and search databases into 80 MiB normal-git chunks:

```sh
bash pack/menus/seal_data.sh
```

The script writes `data/quail*.turso.partNN` plus one SHA-256 file per database
and verifies each concatenated chunk stream before reporting `SEAL_OK`.

After cloning or unpacking the committed chunks, reconstruct both databases:

```sh
bash pack/menus/assemble_data.sh
```

Assembly writes through temporary files, verifies each receipt before replacing
that database path, and reports `ASSEMBLE_OK`.
