# Articles workspace

Local Quail deployment for the articles collection.

Large search DB is split for normal git (no Git LFS). Assemble once after clone:

```sh
bash pack/psychology/assemble_data.sh
```

Then:

```sh
# Requires an EmbeddingGemma query embedder (Ollama or other provider).
ollama serve   # if using Ollama and it is not already running
ollama pull embeddinggemma:300m-qat-q8_0

quail run --config "$(pwd)/quail.toml"
```

MCP listens on `http://127.0.0.1:8000`.
