# Psychology pack — ChatGPT Agent brief

One trial = one chat = one question. Use Quail retrieval only.

## Attach (Library)

1. `psychology-base-quail.zip` (full pack — not mini)
2. `ollama-runtime.zip`
3. `embeddinggemma-q8-model.zip`
4. `quail-wheel.zip`

Split across messages if the ~512MB per-message attach budget requires it.

## Boot

```sh
bash /mnt/data/quail-wheel*/boot_eval.sh /mnt/data
# if nested unzip: bash /mnt/data/quail-wheel*/quail-wheel/boot_eval.sh /mnt/data
```

Wait for `EVAL_READY`. MCP: `http://127.0.0.1:8000/mcp`.

## Answer the question

1. Call `quail_setup` once.
2. Call `quail_exec` on dataset `articles` with Semantic (and Lexical if useful).
3. Answer **only** the pasted question. Cite entry ids and short quotes from hits.
4. Do **not** browse the open web. Do **not** invent corpus evidence.

## Do not

- Run smoke scripts or canned “cognitive dissonance” probes during an eval trial.
- Use `psychology-mini-quail` for scored answers.
- Read or request operator handoff notes (those are for Cursor operators, not this Agent).
