# earth_science articles

Attach both zips and unzip into the same folder:
  1) earth_science_2-base-quail.zip
  2) earth_science_2-search-parts.zip

Then:

Before starting, make sure the supplied Ollama runtime is listening locally and
the supplied embedding model is available to it.

```sh
bash start.sh > quail.log 2>&1 &
```

MCP: `http://127.0.0.1:8000/mcp`.

The attached files are only for installing and running the retrieval service.
Do not inspect or query the raw corpus, database, search index, wheel, or source
code directly. Retrieve evidence through MCP. When reporting important documents,
use the canonical `entry_id` values returned by retrieval, not result handles.
