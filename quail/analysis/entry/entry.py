"""Runtime-issued entry handles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quail.analysis.errors import QuailSyntaxError
from quail.analysis.field import Field

# Private capability: only make_entry / engine may construct Entry instances.
_ENTRY_TOKEN = object()


@dataclass(frozen=True, slots=True)
class Entry:
    """Opaque handle for one dataset row. Issued by retrieve, not user code."""

    id: str
    dataset_id: str = ""
    dataset_version_id: str = ""
    dataset: str = ""

    def __init__(
        self,
        entry_id: str,
        *,
        dataset_id: str = "",
        dataset_version_id: str = "",
        dataset: str = "",
        token: object | None = None,
    ) -> None:
        _require_host_token(token)
        _require_nonempty_entry_id(entry_id)
        # frozen dataclass: must set fields via object.__setattr__
        object.__setattr__(self, "id", entry_id)
        object.__setattr__(self, "dataset_id", dataset_id)
        object.__setattr__(self, "dataset_version_id", dataset_version_id)
        object.__setattr__(self, "dataset", dataset)

    def to_record(self) -> dict[str, Any]:
        """Plain dict for debugging / later serialization."""

        return {
            "id": self.id,
            "dataset_id": self.dataset_id,
            "dataset_version_id": self.dataset_version_id,
            "dataset": self.dataset,
        }

    def value(self, field: Field | str, default: Any = None) -> Any:
        """Return one cell value during worker evaluation via host RPC."""

        from quail.analysis.worker.runtime.runtime import entry_value_rpc

        return entry_value_rpc(self, field, default)

    def fields(self) -> list[Field]:
        """Return present fields on this row during worker evaluation via host RPC."""

        from quail.analysis.worker.runtime.runtime import entry_fields_rpc

        return entry_fields_rpc(self)


def make_entry(
    entry_id: str,
    *,
    dataset_id: str = "",
    dataset_version_id: str = "",
    dataset: str = "",
) -> Entry:
    """Host/runtime helper to mint an Entry handle (not for agent code)."""

    return Entry(
        entry_id,
        dataset_id=dataset_id,
        dataset_version_id=dataset_version_id,
        dataset=dataset,
        token=_ENTRY_TOKEN,
    )


def _require_host_token(token: object | None) -> None:
    if token is not _ENTRY_TOKEN:
        raise QuailSyntaxError("Entry handles are created by Quail, not user code")


def _require_nonempty_entry_id(entry_id: Any) -> None:
    if not isinstance(entry_id, str) or not entry_id:
        raise QuailSyntaxError("Entry id must be a non-empty string")
