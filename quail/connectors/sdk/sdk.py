"""Public connector author types and protocols."""

from __future__ import annotations

import keyword
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

_EXTENSION_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SURFACE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{0,255}\Z")
_SCHEMA_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
_SCHEMA_ROOT_KEYS = frozenset({"additionalProperties", "properties", "required", "type"})
_SCHEMA_COMMON_NODE_KEYS = frozenset({"enum", "type"})
_SCHEMA_OBJECT_NODE_KEYS = frozenset({"additionalProperties", "properties", "required"})
_SCHEMA_ARRAY_NODE_KEYS = frozenset({"items", "maxItems", "minItems", "uniqueItems"})
_MAX_TEXT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolSpec:
    """One MCP tool declared by a connector package."""

    name: str
    title: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None = None
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = False
    meta: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _validate_surface_name(self.name, "tool name")
        _require_bounded_text(self.title, "tool title")
        _require_bounded_text(self.description, "tool description")
        input_schema = _freeze_mapping(self.input_schema, "input schema")
        _validate_schema(input_schema, "input schema", python_parameters=True)
        object.__setattr__(self, "input_schema", input_schema)
        if self.output_schema is not None:
            output_schema = _freeze_mapping(self.output_schema, "output schema")
            _validate_schema(output_schema, "output schema")
            object.__setattr__(self, "output_schema", output_schema)
        if self.meta is not None:
            object.__setattr__(self, "meta", _freeze_mapping(self.meta, "tool meta"))


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """One readable MCP resource declared by a connector package."""

    uri: str
    title: str
    description: str
    mime_type: str = "text/plain"

    def __post_init__(self) -> None:
        _validate_resource_uri(self.uri, "resource URI")
        _require_bounded_text(self.title, "resource title")
        _require_bounded_text(self.description, "resource description")
        _require_bounded_text(self.mime_type, "resource MIME type")


@dataclass(frozen=True, slots=True, kw_only=True)
class WidgetSpec:
    """One MCP UI widget (``ui://`` resource) declared by a connector package."""

    id: str
    uri: str
    compatibility_uris: tuple[str, ...] = ()
    meta: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _validate_surface_name(self.id, "widget id")
        uris = (self.uri, *self.compatibility_uris)
        for uri in uris:
            _validate_resource_uri(uri, "widget URI")
            if not uri.startswith("ui://"):
                raise ValueError("Widget URIs must use the ui:// scheme")
        _require_unique(uris, "widget URIs")
        object.__setattr__(self, "uri", uris[0])
        object.__setattr__(self, "compatibility_uris", uris[1:])
        if self.meta is not None:
            object.__setattr__(self, "meta", _freeze_mapping(self.meta, "widget meta"))


@dataclass(frozen=True, slots=True)
class ConnectorManifest:
    """Invariant package surface: id, version, tools, resources, widgets."""

    id: str
    version: str
    tools: tuple[ToolSpec, ...] = ()
    resources: tuple[ResourceSpec, ...] = ()
    widgets: tuple[WidgetSpec, ...] = ()
    config_keys: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not _EXTENSION_ID.fullmatch(self.id):
            raise ValueError("Connector id must be lowercase letters, digits, and underscores")
        _require_bounded_text(self.version, "connector version", maximum_bytes=128)
        _require_unique((tool.name for tool in self.tools), "tool names")
        _require_unique((resource.uri for resource in self.resources), "resource URIs")
        _require_unique((widget.id for widget in self.widgets), "widget ids")
        object.__setattr__(self, "config_keys", frozenset(self.config_keys))


@dataclass(frozen=True, slots=True)
class DatasetRef:
    """Bounded dataset identity returned by ConnectorHost."""

    id: str
    name: str | None
    active_version_id: str | None


@dataclass(frozen=True, slots=True)
class ConnectorContext:
    """Request authority for one connector invocation."""

    workspace_id: str
    user_id: str | None
    extension_id: str
    extension_version: str
    dataset_ids: frozenset[str] = field(default_factory=frozenset)

    def require_dataset(self, dataset_id: str) -> None:
        if dataset_id not in self.dataset_ids:
            raise PermissionError("The requested connector dataset is not available")


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Structured connector tool result."""

    structured_content: Mapping[str, Any]
    text: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "structured_content",
            _freeze_mapping(self.structured_content, "tool structured content"),
        )
        if self.text is not None:
            _require_bounded_text(self.text, "tool text")


class ConnectorError(Exception):
    """Stable connector failure for agents and operators."""

    def __init__(
        self,
        stable_code: str,
        message: str,
        repair_hint: str,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stable_code = stable_code
        self.message = message
        self.repair_hint = repair_hint
        self.context = dict(context or {})


class ConnectorHost(Protocol):
    """Tiny host surface available to connectors."""

    def dataset(self, context: ConnectorContext, dataset_id: str) -> DatasetRef: ...

    def require_dataset(self, context: ConnectorContext, dataset_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ConnectorEnvironment:
    """Deployment-wide services shared by every workspace connection."""

    host: ConnectorHost
    public_base_url: str
    base_path: Path


@dataclass(frozen=True, slots=True)
class WorkspaceConnectorRuntime:
    """Resolved connection for one connector in one workspace."""

    workspace_id: str
    config: Mapping[str, Any]
    dataset_ids: frozenset[str]


class ConnectorFactory(Protocol):
    def __call__(self, environment: ConnectorEnvironment) -> Connector: ...


class Connector(Protocol):
    """Package-level surface constructed once per loaded identity."""

    @property
    def manifest(self) -> ConnectorManifest: ...

    def read_resource(self, uri: str) -> str: ...

    def connect(self, runtime: WorkspaceConnectorRuntime) -> Provider: ...


class Provider(Protocol):
    """Workspace-scoped behavior; may serve concurrent calls."""

    def call_tool(
        self,
        context: ConnectorContext,
        name: str,
        arguments: Mapping[str, Any],
    ) -> ToolResult | Mapping[str, Any]: ...

    def dataset_document(self, context: ConnectorContext, dataset_id: str) -> str | None: ...


def _require_bounded_text(value: str, label: str, *, maximum_bytes: int = _MAX_TEXT_BYTES) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds {maximum_bytes} bytes")


def _validate_surface_name(value: str, label: str) -> None:
    if not isinstance(value, str) or not _SURFACE_NAME.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _validate_resource_uri(value: str, label: str) -> None:
    _validate_surface_name(value, label)
    scheme, separator, remainder = value.partition("://")
    authority = remainder.partition("/")[0]
    path_segments = remainder.partition("/")[2].split("/") if "/" in remainder else []
    if (
        not separator
        or not authority
        or scheme != scheme.casefold()
        or authority != authority.casefold()
        or any(segment in {".", ".."} for segment in path_segments)
    ):
        raise ValueError(
            f"{label} must use a canonical lowercase scheme, authority, and path"
        )


def _require_unique(values: Any, label: str) -> None:
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"Duplicate {label}: {value!r}")
        seen.add(value)


def _freeze_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or isinstance(value, str | bytes):
        raise ValueError(f"{label} must be an object")
    return MappingProxyType(dict(value))


def _validate_schema(
    schema: Mapping[str, Any],
    label: str,
    *,
    python_parameters: bool = False,
) -> None:
    if schema.get("type") != "object":
        raise ValueError(f"Connector {label} must describe an object")
    _validate_schema_node(schema, label, root=True, python_parameters=python_parameters)


def _validate_schema_node(
    schema: Mapping[str, Any],
    label: str,
    *,
    root: bool = False,
    python_parameters: bool = False,
) -> None:
    expected_types = _validate_schema_types(schema.get("type"), label)
    allowed_keys = set(_SCHEMA_ROOT_KEYS if root else _SCHEMA_COMMON_NODE_KEYS)
    if not root:
        if "object" in expected_types:
            allowed_keys.update(_SCHEMA_OBJECT_NODE_KEYS)
        if "array" in expected_types:
            allowed_keys.update(_SCHEMA_ARRAY_NODE_KEYS)
    unsupported = sorted(set(schema) - allowed_keys)
    if unsupported:
        rendered = ", ".join(repr(key) for key in unsupported)
        raise ValueError(f"Connector {label} contains unsupported schema keywords: {rendered}")
    if "object" in expected_types:
        _validate_object_schema_node(schema, label, python_parameters=python_parameters)
    if "array" in expected_types:
        items = schema.get("items")
        if not isinstance(items, Mapping):
            raise ValueError(f"Connector {label} array nodes require an object items schema")
        _validate_schema_node(items, f"{label} items")
        for key in ("minItems", "maxItems"):
            if key in schema and (
                isinstance(schema[key], bool) or not isinstance(schema[key], int) or schema[key] < 0
            ):
                raise ValueError(f"Connector {label} {key} must be a non-negative integer")
        if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
            raise ValueError(f"Connector {label} uniqueItems must be a boolean")
    if "enum" in schema:
        enum_values = schema["enum"]
        if not isinstance(enum_values, list) or not enum_values:
            raise ValueError(f"Connector {label} enum must be a non-empty list")


def _validate_schema_types(value: Any, label: str) -> set[str]:
    if isinstance(value, str):
        types = {value}
    elif isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        types = set(value)
    else:
        raise ValueError(f"Connector {label} type must be a string or non-empty list of strings")
    unknown = types - _SCHEMA_TYPES
    if unknown:
        raise ValueError(f"Connector {label} has unsupported types: {sorted(unknown)}")
    return types


def _validate_object_schema_node(
    schema: Mapping[str, Any],
    label: str,
    *,
    python_parameters: bool,
) -> None:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        raise ValueError(f"Connector {label} object nodes require a properties object")
    for key, child in properties.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"Connector {label} property names must be non-empty strings")
        if python_parameters and (keyword.iskeyword(key) or not key.isidentifier()):
            raise ValueError(f"Connector {label} property {key!r} is not a valid Python parameter")
        if not isinstance(child, Mapping):
            raise ValueError(f"Connector {label} property {key!r} must be an object schema")
        _validate_schema_node(child, f"{label}.{key}", python_parameters=False)
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ValueError(f"Connector {label} required must be a list of strings")
    missing = [item for item in required if item not in properties]
    if missing:
        raise ValueError(f"Connector {label} required unknown properties: {missing}")
    additional = schema.get("additionalProperties", False)
    if not isinstance(additional, bool):
        raise ValueError(f"Connector {label} additionalProperties must be a boolean")
    if additional is not False:
        raise ValueError(f"Connector {label} must set additionalProperties to false")