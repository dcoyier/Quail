# Connector SDK

Author guide for Quail v0.11 trusted connectors. Import **only**
`quail.connectors.sdk`. Do not import `quail.connectors.load`, MCP internals,
CoreDb, search, or analysis modules from connector packages.

## What a connector is

```text
connector package =
  tools
  dataset docs
  widgets (MCP UI)
  + thin glue (manifest, errors, context)
```

Operators install a wheel, pin it in hand-edited TOML, and activate it per
workspace. Quail finds packages via entry points (`quail.connectors`), not
file paths in TOML. Hosting / ngrok stays outside the SDK
(`hosting.public_base_url`).

## Public types

| Kind | Symbols |
| --- | --- |
| Declarations | `ConnectorManifest`, `ToolSpec`, `ResourceSpec`, `WidgetSpec`, `RouteSpec` |
| Lifecycle | `ConnectorFactory`, `Connector`, `Provider` |
| Env / request | `ConnectorEnvironment`, `WorkspaceConnectorRuntime`, `ConnectorContext`, `ConnectorHost`, `DatasetRef` |
| Results | `ToolResult`, `ToolImage`, `ConnectorError`, `FileResponse` |

### Required methods

- **Connector:** `manifest`, `read_resource(uri)`, `connect(runtime) -> Provider`
- **Provider:** `call_tool(context, name, args)`, `dataset_document(context, dataset_id) -> str | None`,
  `handle_route(context, route_id, path_params) -> FileResponse | None`
- **ConnectorHost (v1):** `dataset`, `require_dataset` only

Tool input schemas must be closed JSON Schema objects (`type: object`,
`additionalProperties: false`) with Python-safe property names.

`ToolResult` may attach up to eight `ToolImage` values (PNG/JPEG/WebP/GIF,
≤512 KiB each). The host maps them to MCP `ImageContent` blocks after any text
block; omit `text` with a non-empty `images` tuple for image-only content.
Optional `text` may be up to 2 MiB (for connectors that embed large payloads
such as preview base64). `structuredContent` stays JSON-only.

## Lifecycle

1. Operator pins `[[extensions]]` id + version (deployment-wide).
2. Operator activates with `[[connectors]]` (unrestricted) or
   `[[workspaces.connectors]]` (Clerk), including optional `config` and
   `datasets` bindings.
3. On `quail run`, Quail loads the entry point, checks version match, connects
   each workspace binding, and **fails closed** if two+ bound connectors both
   return documentation for the same `dataset_id`.
4. MCP registers tools / resources / widgets. `quail_get_dataset_info` calls
   the owning provider’s `dataset_document` when present.

Tools may overlap on the same dataset; only competing **docs** are rejected.

## TOML shape

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

Unknown keys in `config` must be listed on `ConnectorManifest.config_keys` or
`quail run` fails. An empty `config_keys` allowlist permits no config keys.
Secrets ceremony is deferred; keep config JSON-shaped and small.

## Dataset documentation

Return a non-empty string from `dataset_document` for ids you own; return
`None` for ids you do not document. Empty strings are rejected at load.
Guidance is SDK-only — there is no TOML `info=` field.

## Widgets

Declare `WidgetSpec` with `ui://…` URIs. Host serves them as MCP resources
(`text/html;profile=mcp-app`). Prefer in-MCP widgets for UI.

## HTTP routes (narrow)

Declare `RouteSpec` for trusted file delivery (for example short-lived signed
asset downloads). The host mounts:

`/extensions/{connector_id}/{workspace_id}/{path_suffix}`

GET only. Implement `Provider.handle_route(context, route_id, path_params)` and
return `FileResponse` or `None`. Do not use routes as a general app server —
widgets and tools remain the agent-facing surface.

In Clerk mode the host requires a Bearer token whose TOML user is a member of
the URL `workspace_id`; missing/invalid tokens are 401 and non-members are 403.
Unrestricted mode does not authenticate these GETs. `FileResponse.expires_at`,
when set, is enforced: an expired timestamp is 404, not a file.

## Example

See [`examples/notes-connector/`](../examples/notes-connector/) for a runnable
package (tool + notes docs + widget) and install notes.
## Fail-closed operator errors

| Situation | Behavior |
| --- | --- |
| Missing entry point / package | `quail run` fails |
| TOML version ≠ manifest / distribution | `quail run` fails |
| Two connectors document the same bound `dataset_id` | `quail run` fails |
| Connector active only in workspace A | Does not appear in workspace B |
