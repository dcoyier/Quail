# Articles workspace

Local Quail deployment for the articles collection.

```sh
# Requires Ollama with EmbeddingGemma for semantic query embedding.
ollama serve   # if not already running
ollama pull embeddinggemma:300m-qat-q8_0

quail run --config "$(pwd)/quail.toml"
```

MCP listens on `http://127.0.0.1:8000`.
