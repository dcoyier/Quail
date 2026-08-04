# NYPL menu-document pack

Menu-level companion to `pack/menus` from the same pinned NYPL *What's on the
Menu?* export. Each row is one historical menu; `body` is the stripped
`menu_text` with dish lines in `(page_number, ypos)` reading order.

## Build

```sh
python examples/nypl-menus/prepare.py
python pack/menu_docs/prepare_menu_docs.py
```

This writes:

- gitignored `data/menu_docs.csv`: 17,514 menu rows with slim menu metadata;
- `pack/menu_docs/shards/shard-00…19.jsonl`: 17,501 unique nonempty bodies.

Shard `00` has 876 rows. Shards `01` through `19` have 875 rows each.

## Embedding contract

- Field: `body` only
- Body: exact stripped `menu_text`
- `text_hash`: SHA-256 of that exact UTF-8 body
- Model: `embeddinggemma:300m-qat-q8_0`
- Dimensions: 768
- Revision: `embeddinggemma-300m-qat-q8-v1`

Each worker owns exactly one shard and writes only its matching gzip under
`pack/menu_docs/vectors/`.

```sh
python pack/menu_docs/embed_worker.py \
  --ensure-ollama \
  --shard pack/menu_docs/shards/shard-00.jsonl \
  --out pack/menu_docs/vectors/shard-00.jsonl.gz \
  --model embeddinggemma:300m-qat-q8_0 \
  --dimensions 768 \
  --batch-size 32
```

The input and output line counts must match before committing the vector file.

## Combined warm

Root `quail.toml` declares both `dishes` and `menu_docs`. Pass both vector
directories to the shared warmer so a clean build can satisfy both datasets:

```sh
python pack/tools/warm_precomputed.py \
  --config "$(pwd)/quail.toml" \
  --vectors-dir "$(pwd)/pack/menus/vectors" \
  --vectors-dir "$(pwd)/pack/menu_docs/vectors"
```

The verified result has 17,514 menu-document Lexical `body` texts and 17,501
unique menu-document vectors. Seal the combined databases with
`bash pack/menus/seal_data.sh`.
