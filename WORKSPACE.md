# Articles workspace

Local Quail deployment for the articles collection.

```sh
# Requires Ollama with embeddinggemma:300m for semantic query embedding.
ollama serve   # if not already running
ollama pull embeddinggemma:300m

quail run --config "$(pwd)/quail.toml"
```

MCP listens on `http://127.0.0.1:8000`.
