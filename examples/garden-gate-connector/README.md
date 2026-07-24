# Garden Gate connector (example)

Minimal Quail v0.11 connector: one tool, `garden-gate` dataset docs, one MCP UI widget.

## Install into Quail’s environment

From the Quail v0.11 checkout (editable):

```bash
uv pip install -e ./examples/garden-gate-connector
```

Or build a wheel and install that wheel into the same venv Quail uses.

## Pin and connect in `quail.toml`

Unrestricted example:

```toml
[[extensions]]
id = "garden_gate"
version = "1.0.0"

[[connectors]]
id = "garden_gate"

[connectors.config]
heading = "Garden Gate"

[[connectors.datasets]]
id = "garden-gate"
```

Clerk mode uses the same shape under a workspace:

```toml
[[extensions]]
id = "garden_gate"
version = "1.0.0"

[[workspaces]]
id = "demo"

[[workspaces.connectors]]
id = "garden_gate"

[workspaces.connectors.config]
heading = "Garden Gate"

[[workspaces.connectors.datasets]]
id = "garden-gate"
```

Then `quail process` / `quail run` as usual. The CLI never writes this TOML.

## What agents see

- Tool `garden_gate_describe_dataset`
- Docs via `quail_get_dataset_info` for dataset id `garden-gate`
- Widget resource `ui://garden-gate/dataset-card.html`

Author surface: import only `quail.connectors.sdk` (see `docs/connector-sdk.md`).
