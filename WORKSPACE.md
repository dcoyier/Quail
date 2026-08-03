# Articles workspace

Local Quail deployment for the stackoverflow articles collection.

```sh
bash pack/stackoverflow/assemble_data.sh
quail run --config "$(pwd)/quail.toml"
```

MCP: `http://127.0.0.1:8000/mcp`.
