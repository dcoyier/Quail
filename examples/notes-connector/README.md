# Notes connector (example)

Minimal Quail v0.11 connector: one tool, `notes` dataset docs, one MCP UI widget.

## Install into Quail’s environment

From the Quail v0.11 checkout (editable):

```bash
uv pip install -e ./examples/notes-connector
```

Or build a wheel and install that wheel into the same venv Quail uses.

## Pin and connect in `quail.toml`

Unrestricted example:

```toml
[[extensions]]
id = "notes"
version = "1.0.0"

[[connectors]]
id = "notes"

[connectors.config]
heading = "Notes"

[[connectors.datasets]]
id = "notes"
```

Clerk mode uses the same shape under a workspace:

```toml
[[extensions]]
id = "notes"
version = "1.0.0"

[[workspaces]]
id = "demo"

[[workspaces.connectors]]
id = "notes"

[workspaces.connectors.config]
heading = "Notes"

[[workspaces.connectors.datasets]]
id = "notes"
```

Then `quail run` (connectors attach at run, not process). CSV import still
uses `quail process`. The CLI never writes this TOML.

## What agents see

- Tool `notes_describe_dataset`
- Docs via `quail_get_dataset_info` for dataset id `notes`
- Widget resource `ui://notes/dataset-card.html`

Author surface: import only `quail.connectors.sdk` (see `docs/connector-sdk.md`).
