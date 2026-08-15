"""Load and connect TOML-pinned connector packages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, entry_points, version as package_version
from pathlib import Path
from types import MappingProxyType

from quail.config.errors import ConfigError
from quail.config.models import ExtensionPin, QuailConfig, WorkspaceSpec
from quail.connectors.sdk import (
    Connector,
    ConnectorContext,
    ConnectorEnvironment,
    ConnectorError,
    ConnectorFactory,
    ConnectorManifest,
    DatasetRef,
    Provider,
    WorkspaceConnectorRuntime,
)
from quail.datasets import get_dataset, open_core_db
from quail.datasets.db import CoreDb

ENTRY_POINT_GROUP = "quail.connectors"


class ConnectorLoadError(ConfigError):
    """Fail-closed connector discovery or ownership error at quail run."""


@dataclass(slots=True)
class ConnectedProvider:
    """One workspace-connected provider with its manifest and bindings."""

    extension_id: str
    version: str
    manifest: ConnectorManifest
    connector: Connector
    provider: Provider
    dataset_ids: frozenset[str]
    provides_docs: bool


@dataclass(slots=True)
class WorkspaceConnectorBundle:
    """All connected providers for one workspace."""

    workspace_id: str
    providers: tuple[ConnectedProvider, ...]
    docs_by_dataset: Mapping[str, ConnectedProvider]


@dataclass(slots=True)
class ConnectorCatalog:
    """Loaded connectors for a deployment, keyed by workspace."""

    by_workspace: dict[str, WorkspaceConnectorBundle] = field(default_factory=dict)

    def for_workspace(self, workspace_id: str) -> WorkspaceConnectorBundle:
        return self.by_workspace.get(
            workspace_id,
            WorkspaceConnectorBundle(workspace_id=workspace_id, providers=(), docs_by_dataset={}),
        )

    def close(self) -> None:
        """Best-effort disconnect for connectors that implement disconnect()."""

        seen: set[int] = set()
        for bundle in self.by_workspace.values():
            for connected in bundle.providers:
                connector = connected.connector
                identity = id(connector)
                if identity in seen:
                    continue
                seen.add(identity)
                disconnect = getattr(connector, "disconnect", None)
                if callable(disconnect):
                    disconnect()


@dataclass(slots=True)
class _CoreHost:
    """ConnectorHost backed by CoreDb catalog reads."""

    db_path: Path

    def dataset(self, context: ConnectorContext, dataset_id: str) -> DatasetRef:
        self.require_dataset(context, dataset_id)
        with open_core_db(self.db_path) as db:
            ref = get_dataset(db, context.workspace_id, dataset_id)
        if ref is None:
            raise ConnectorError(
                "UNKNOWN_DATASET",
                f"Dataset {dataset_id!r} is not in this workspace catalog.",
                "Import that dataset in quail.toml, run quail process, then "
                "restart quail run.",
            )
        return DatasetRef(
            id=ref.dataset_id,
            name=ref.name,
            active_version_id=ref.active_version_id,
        )

    def require_dataset(self, context: ConnectorContext, dataset_id: str) -> None:
        context.require_dataset(dataset_id)


def load_connector_catalog(config: QuailConfig, db: CoreDb) -> ConnectorCatalog:
    """Discover pinned packages, connect per workspace, fail closed on conflicts."""

    packages = _load_pinned_packages(config.extensions, base_path=config.manifest_path.parent)
    environment = ConnectorEnvironment(
        host=_CoreHost(db_path=db.path),
        public_base_url=config.public_base_url,
        base_path=config.manifest_path.parent,
    )
    connectors: dict[str, tuple[Connector, ConnectorManifest]] = {}
    for pin in config.extensions:
        factory, dist_version = packages[pin.extension_id]
        connector = factory(environment)
        manifest = connector.manifest
        if manifest.id != pin.extension_id:
            raise ConnectorLoadError(
                f"Connector entry point {pin.extension_id!r} declared manifest id "
                f"{manifest.id!r}"
            )
        if manifest.version != pin.version or dist_version != pin.version:
            raise ConnectorLoadError(
                f"Connector {pin.extension_id!r} version mismatch: TOML={pin.version!r}, "
                f"manifest={manifest.version!r}, distribution={dist_version!r}"
            )
        connectors[pin.extension_id] = (connector, manifest)

    catalog = ConnectorCatalog()
    for workspace in config.workspaces:
        catalog.by_workspace[workspace.workspace_id] = _connect_workspace(
            workspace,
            connectors=connectors,
        )
    return catalog


def _load_pinned_packages(
    pins: tuple[ExtensionPin, ...],
    *,
    base_path: Path,
) -> dict[str, tuple[ConnectorFactory, str]]:
    del base_path  # reserved for future path installs; pins are entry-point only
    if not pins:
        return {}
    eps = entry_points().select(group=ENTRY_POINT_GROUP)
    by_name = {ep.name: ep for ep in eps}
    loaded: dict[str, tuple[ConnectorFactory, str]] = {}
    for pin in pins:
        ep = by_name.get(pin.extension_id)
        if ep is None:
            raise ConnectorLoadError(
                f"No {ENTRY_POINT_GROUP} entry point named {pin.extension_id!r}. "
                "Install the connector wheel into Quail's environment."
            )
        try:
            dist_version = package_version(ep.dist.name) if ep.dist is not None else pin.version
        except PackageNotFoundError as error:
            raise ConnectorLoadError(
                f"Connector package for {pin.extension_id!r} is not installed"
            ) from error
        if dist_version != pin.version:
            raise ConnectorLoadError(
                f"Connector {pin.extension_id!r} installed version {dist_version!r} "
                f"does not match TOML pin {pin.version!r}"
            )
        factory = ep.load()
        if not callable(factory):
            raise ConnectorLoadError(
                f"Connector entry point {pin.extension_id!r} must be a callable factory"
            )
        loaded[pin.extension_id] = (factory, dist_version)
    return loaded


def _connect_workspace(
    workspace: WorkspaceSpec,
    *,
    connectors: dict[str, tuple[Connector, ConnectorManifest]],
) -> WorkspaceConnectorBundle:
    connected: list[ConnectedProvider] = []
    docs_owners: dict[str, list[str]] = {}

    for binding in workspace.connectors:
        connector, manifest = connectors[binding.extension_id]
        unknown = set(binding.config) - set(manifest.config_keys)
        if unknown:
            raise ConnectorLoadError(
                f"Connector {binding.extension_id!r} rejected unknown config keys: "
                f"{sorted(unknown)}"
            )
        runtime = WorkspaceConnectorRuntime(
            workspace_id=workspace.workspace_id,
            config=MappingProxyType(dict(binding.config)),
            dataset_ids=frozenset(binding.dataset_ids),
        )
        provider = connector.connect(runtime)
        doc_datasets = _provider_doc_datasets(
            provider,
            workspace_id=workspace.workspace_id,
            extension_id=binding.extension_id,
            version=manifest.version,
            dataset_ids=frozenset(binding.dataset_ids),
        )
        connected.append(
            ConnectedProvider(
                extension_id=binding.extension_id,
                version=manifest.version,
                manifest=manifest,
                connector=connector,
                provider=provider,
                dataset_ids=frozenset(binding.dataset_ids),
                provides_docs=bool(doc_datasets),
            )
        )
        for dataset_id in doc_datasets:
            docs_owners.setdefault(dataset_id, []).append(binding.extension_id)

    conflicts = {
        dataset_id: owners for dataset_id, owners in docs_owners.items() if len(owners) > 1
    }
    if conflicts:
        parts = [
            f"{dataset_id} <- {', '.join(owners)}"
            for dataset_id, owners in sorted(conflicts.items())
        ]
        raise ConnectorLoadError(
            "Multiple connectors provide dataset documentation for the same dataset_id "
            f"in workspace {workspace.workspace_id!r}: " + "; ".join(parts)
        )

    docs_by_dataset = {
        dataset_id: next(
            item for item in connected if item.extension_id == owners[0] and item.provides_docs
        )
        for dataset_id, owners in docs_owners.items()
    }
    return WorkspaceConnectorBundle(
        workspace_id=workspace.workspace_id,
        providers=tuple(connected),
        docs_by_dataset=docs_by_dataset,
    )


def _provider_doc_datasets(
    provider: Provider,
    *,
    workspace_id: str,
    extension_id: str,
    version: str,
    dataset_ids: frozenset[str],
) -> frozenset[str]:
    """Dataset ids for which the provider returns non-empty documentation."""

    if not dataset_ids:
        return frozenset()
    context = ConnectorContext(
        workspace_id=workspace_id,
        user_id=None,
        extension_id=extension_id,
        extension_version=version,
        dataset_ids=dataset_ids,
    )
    documented: set[str] = set()
    for dataset_id in dataset_ids:
        try:
            document = provider.dataset_document(context, dataset_id)
        except Exception as error:
            raise ConnectorLoadError(
                f"Connector {extension_id!r} dataset_document({dataset_id!r}) failed during "
                f"quail run: {error}"
            ) from error
        if document is None:
            continue
        if not isinstance(document, str) or not document.strip():
            raise ConnectorLoadError(
                f"Connector {extension_id!r} returned empty dataset documentation for "
                f"{dataset_id!r}"
            )
        documented.add(dataset_id)
    return frozenset(documented)


def make_request_context(
    *,
    workspace_id: str,
    user_id: str | None,
    connected: ConnectedProvider,
) -> ConnectorContext:
    """Build a ConnectorContext for an MCP tool/resource/docs call."""

    return ConnectorContext(
        workspace_id=workspace_id,
        user_id=user_id,
        extension_id=connected.extension_id,
        extension_version=connected.version,
        dataset_ids=connected.dataset_ids,
    )


def resolve_dataset_documentation(
    bundle: WorkspaceConnectorBundle,
    *,
    workspace_id: str,
    user_id: str | None,
    dataset_id: str,
) -> str | None:
    """Return documentation text for dataset_id, or None."""

    owner = bundle.docs_by_dataset.get(dataset_id)
    if owner is None:
        return None
    context = make_request_context(
        workspace_id=workspace_id,
        user_id=user_id,
        connected=owner,
    )
    return owner.provider.dataset_document(context, dataset_id)
