"""Notes example connector for Quail v0.11.

Imports only ``quail.connectors.sdk``. Used as the author smoke oracle.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from typing import Any

from quail.connectors.sdk import (
    ConnectorContext,
    ConnectorEnvironment,
    ConnectorError,
    ConnectorHost,
    ConnectorManifest,
    DatasetRef,
    ResourceSpec,
    ToolResult,
    ToolSpec,
    WidgetSpec,
    WorkspaceConnectorRuntime,
)

_TOOL_NAME = "notes_describe_dataset"
_ABOUT_URI = "quail://notes/about"
_WIDGET_URI = "ui://notes/dataset-card.html"
_WIDGET_COMPATIBILITY_URI = "ui://notes/dataset-card-v1.html"
_CONFIG_KEYS = frozenset({"heading"})
_NOTES_DATASET = "notes"

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dataset_id": {"type": "string"},
        "include_version": {"type": "boolean"},
    },
    "required": ["dataset_id"],
    "additionalProperties": False,
}
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dataset_id": {"type": "string"},
        "name": {"type": ["string", "null"]},
        "active_version_id": {"type": ["string", "null"]},
        "heading": {"type": "string"},
    },
    "required": ["dataset_id", "name", "active_version_id", "heading"],
    "additionalProperties": False,
}

_NOTES_DOCS = """\
## Notes corpus

Small sample CSV (`id`, `title`, `body`) for connector and analysis smoke tests.

- Prefer `body` for Lexical and Semantic search and for `RegexSearch`.
- `title` is a short label; `id` is stable across imports.
- Imported source data is immutable; analysis tags are per dataset version;
  bindings stay on the session.

Use `{tool}` for a short card, then `quail_exec` with `quail_get_api_docs`.
""".format(tool=_TOOL_NAME)


class NotesProvider:
    """Workspace-scoped behavior; one instance may serve concurrent calls."""

    def __init__(
        self,
        host: ConnectorHost,
        workspace_id: str,
        heading: str,
    ) -> None:
        self._host = host
        self._workspace_id = workspace_id
        self._heading = heading

    def call_tool(
        self,
        context: ConnectorContext,
        name: str,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        if name != _TOOL_NAME:
            raise ConnectorError(
                "UNKNOWN_TOOL",
                "The notes connector does not provide that tool.",
                "Refresh the available tool list and retry.",
            )
        dataset_id = arguments.get("dataset_id")
        include_version = arguments.get("include_version", True)
        if not isinstance(dataset_id, str) or not isinstance(include_version, bool):
            raise ConnectorError(
                "INVALID_ARGUMENTS",
                "The notes connector received invalid arguments.",
                "Pass a dataset_id and an optional boolean include_version.",
            )
        dataset = self._authorized_dataset(context, dataset_id)
        payload = {
            "dataset_id": dataset.id,
            "name": dataset.name,
            "active_version_id": dataset.active_version_id if include_version else None,
            "heading": self._heading,
        }
        return ToolResult(
            structured_content=payload,
            text=(
                f"{self._heading}: {dataset.name or dataset.id}. "
                "Prefer body for Lexical, Semantic, and RegexSearch."
            ),
        )

    def dataset_document(self, context: ConnectorContext, dataset_id: str) -> str | None:
        if dataset_id != _NOTES_DATASET:
            return None
        self._authorized_dataset(context, dataset_id)
        return f"## {self._heading}\n\n{_NOTES_DOCS}"

    def handle_route(
        self,
        context: ConnectorContext,
        route_id: str,
        path_params: Mapping[str, Any],
    ):
        del context, route_id, path_params
        return None

    def _authorized_dataset(self, context: ConnectorContext, dataset_id: str) -> DatasetRef:
        if context.workspace_id != self._workspace_id:
            raise PermissionError("The request does not belong to this workspace provider")
        return self._host.dataset(context, dataset_id)


class NotesConnector:
    """Package-level declarations and static resources shared by workspaces."""

    def __init__(self, environment: ConnectorEnvironment) -> None:
        self._host = environment.host
        self._manifest = ConnectorManifest(
            id="notes",
            version="1.0.0",
            tools=(
                ToolSpec(
                    name=_TOOL_NAME,
                    title="Describe a bound notes dataset",
                    description="Return a small card for one currently authorized dataset.",
                    input_schema=_INPUT_SCHEMA,
                    output_schema=_OUTPUT_SCHEMA,
                    read_only=True,
                    destructive=False,
                    idempotent=True,
                    open_world=False,
                    meta={"ui": {"resourceUri": _WIDGET_URI}},
                ),
            ),
            resources=(
                ResourceSpec(
                    _ABOUT_URI,
                    "About the notes connector",
                    "Static connector information for operators and agents.",
                    "text/markdown",
                ),
            ),
            widgets=(
                WidgetSpec(
                    id="notes_dataset_card",
                    uri=_WIDGET_URI,
                    compatibility_uris=(_WIDGET_COMPATIBILITY_URI,),
                    meta={
                        "ui": {
                            "prefersBorder": True,
                            "csp": {"connectDomains": [], "resourceDomains": []},
                        }
                    },
                ),
            ),
            config_keys=_CONFIG_KEYS,
        )

    @property
    def manifest(self) -> ConnectorManifest:
        return self._manifest

    def read_resource(self, uri: str) -> str:
        if uri == _ABOUT_URI:
            return (
                "# Notes connector\n\n"
                "Example Quail connector: dataset docs, one tool, one MCP UI widget.\n"
            )
        if uri in {_WIDGET_URI, _WIDGET_COMPATIBILITY_URI}:
            return (
                files("quail_notes_connector")
                .joinpath("widget.html")
                .read_text(encoding="utf-8")
            )
        raise ValueError(f"Unknown notes connector resource: {uri}")

    def connect(self, runtime: WorkspaceConnectorRuntime) -> NotesProvider:
        unknown = sorted(set(runtime.config) - _CONFIG_KEYS)
        if unknown:
            raise ValueError(f"Notes connector rejected unknown config keys: {unknown}")
        heading = runtime.config.get("heading", "Notes")
        if (
            not isinstance(heading, str)
            or not heading.strip()
            or len(heading.encode("utf-8")) > 200
        ):
            raise ValueError("Notes connector heading must be non-empty bounded text")
        return NotesProvider(self._host, runtime.workspace_id, heading.strip())


def create_connector(environment: ConnectorEnvironment) -> NotesConnector:
    """Entry-point factory called once for the installed package identity."""

    return NotesConnector(environment)


__all__ = ["create_connector"]
