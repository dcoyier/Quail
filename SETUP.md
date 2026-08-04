# psychology articles

Before starting, make sure the supplied Ollama runtime is listening locally and
the supplied embedding model is available to it.

```sh
bash start.sh > quail.log 2>&1 &
```

MCP: `http://127.0.0.1:8000/mcp`.

After launching Quail, verify readiness by establishing a normal MCP connection
and successfully completing MCP initialization. Do not use a bare HTTP GET to
`/mcp` as a health check: because `/mcp` is an MCP Streamable HTTP endpoint,
generic GET requests may correctly return HTTP 400 or 406. Once MCP
initialization succeeds, consider Quail ready and continue; do not restart the
server because a plain HTTP request returned a non-200 response.

The attached files are only for installing and running the retrieval service.
Do not inspect or query the raw corpus, database, search index, wheel, or source
code directly. Retrieve evidence through MCP. When reporting important documents,
use the canonical `entry_id` values returned by retrieval, not result handles.
