"""Import and durable-size bounds for datasets."""

from __future__ import annotations

MAX_CSV_BYTES = 64 * 1024 * 1024
MAX_CSV_ROWS = 100_000
MAX_DATASET_FIELDS = 1_024
MAX_DATASET_CELLS = 2_000_000
MAX_DATASET_DURABLE_BYTES = 512 * 1024 * 1024
MAX_SOURCE_FIELD_NAME_BYTES = 512
MAX_SOURCE_ENTRY_ID_BYTES = 512
MAX_DATASET_SCOPE_ID_BYTES = 512
SOURCE_VALUE_DURABLE_ROW_OVERHEAD_BYTES = 256
SOURCE_VALUE_DURABLE_ENTRY_KEY_COPIES = 6
SOURCE_VALUE_DURABLE_FIELD_KEY_COPIES = 5
READ_CHUNK_BYTES = 1024 * 1024


def estimated_source_value_row_bytes(
    entry_id: str,
    field_name: str,
    canonical_value_json: str,
) -> int:
    """Estimate one durable source-value row's logical footprint."""

    return (
        SOURCE_VALUE_DURABLE_ROW_OVERHEAD_BYTES
        + SOURCE_VALUE_DURABLE_ENTRY_KEY_COPIES * len(entry_id.encode("utf-8"))
        + SOURCE_VALUE_DURABLE_FIELD_KEY_COPIES * len(field_name.encode("utf-8"))
        + len(canonical_value_json.encode("utf-8"))
    )
