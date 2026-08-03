# Articles workspace

Local Quail deployment for the articles collection.

```sh
bash assemble.sh
# Start Ollama with EmbeddingGemma q8 (bundled model store), then:
quail run --config "$(pwd)/quail.toml"
```

MCP: `http://127.0.0.1:8000/mcp`.
