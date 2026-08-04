# earth_science articles

Attach both zips and unzip into the same folder:
  1) earth_science_2-base-quail.zip
  2) earth_science_2-search-parts.zip

Then:

```sh
pip install --no-index --find-links=./deps ./quail-0.11.0a0-py3-none-any.whl
bash assemble.sh
quail run --config "$(pwd)/quail.toml"
```

MCP: `http://127.0.0.1:8000/mcp`.
