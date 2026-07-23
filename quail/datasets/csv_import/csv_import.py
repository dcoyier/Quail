"""UTF-8 CSV loader for immutable dataset versions."""

from __future__ import annotations

import csv
import io
import os
import stat
from pathlib import Path
from typing import Any, BinaryIO

from quail.datasets.errors import DatasetSyntaxError
from quail.datasets.hashing import canonical_json, dataset_content_hash
from quail.datasets.limits import (
    MAX_CSV_BYTES,
    MAX_CSV_ROWS,
    MAX_DATASET_CELLS,
    MAX_DATASET_DURABLE_BYTES,
    MAX_DATASET_FIELDS,
    MAX_SOURCE_ENTRY_ID_BYTES,
    MAX_SOURCE_FIELD_NAME_BYTES,
    READ_CHUNK_BYTES,
    estimated_source_value_row_bytes,
)
from quail.datasets.models import CsvDataset


def load_csv_dataset(path: str | Path) -> CsvDataset:
    """Read one bounded UTF-8 CSV with a required unique ``id`` column."""

    source = Path(os.path.abspath(Path(path).expanduser()))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as raw_handle:
            if not stat.S_ISREG(os.fstat(raw_handle.fileno()).st_mode):
                raise DatasetSyntaxError(f"CSV file does not exist: {source}")
            payload = _read_bounded(raw_handle)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as error:
        raise DatasetSyntaxError(f"CSV file does not exist: {source}") from error

    try:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = reader.fieldnames
        if headers is None:
            raise DatasetSyntaxError("CSV file has no header row")
        if any(not header or not header.strip() for header in headers):
            raise DatasetSyntaxError("CSV headers cannot be empty")
        normalized_headers = [header.strip() for header in headers]
        if any(
            len(header.encode("utf-8")) > MAX_SOURCE_FIELD_NAME_BYTES
            for header in normalized_headers
        ):
            raise DatasetSyntaxError(
                f"CSV headers cannot exceed {MAX_SOURCE_FIELD_NAME_BYTES} UTF-8 bytes"
            )
        if len(normalized_headers) != len(set(normalized_headers)):
            raise DatasetSyntaxError("CSV headers must be unique")
        if "id" not in normalized_headers:
            raise DatasetSyntaxError("CSV file requires an id column")
        field_names = tuple(name for name in normalized_headers if name != "id")
        if len(field_names) > MAX_DATASET_FIELDS:
            raise DatasetSyntaxError(
                f"CSV import exceeds the {MAX_DATASET_FIELDS}-column field limit"
            )

        entries: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        cell_count = 0
        estimated_durable_bytes = 0
        for line_number, raw_row in enumerate(reader, start=2):
            if len(entries) >= MAX_CSV_ROWS:
                raise DatasetSyntaxError(f"CSV import exceeds the {MAX_CSV_ROWS}-row limit")
            if None in raw_row:
                raise DatasetSyntaxError(f"CSV row {line_number} has more values than headers")
            cell_count += len(normalized_headers)
            if cell_count > MAX_DATASET_CELLS:
                raise DatasetSyntaxError(f"CSV import exceeds the {MAX_DATASET_CELLS}-cell limit")

            values_by_header: dict[str, str] = {}
            for original, normalized in zip(headers, normalized_headers, strict=True):
                raw_value = raw_row.get(original)
                if raw_value is None:
                    values_by_header[normalized] = ""
                else:
                    values_by_header[normalized] = str(raw_value).strip()

            entry_id = values_by_header["id"]
            if not entry_id:
                raise DatasetSyntaxError(f"CSV row {line_number} has a blank id")
            if len(entry_id.encode("utf-8")) > MAX_SOURCE_ENTRY_ID_BYTES:
                raise DatasetSyntaxError(
                    f"CSV row {line_number} id cannot exceed "
                    f"{MAX_SOURCE_ENTRY_ID_BYTES} UTF-8 bytes"
                )
            if entry_id in seen_ids:
                raise DatasetSyntaxError(f"CSV row {line_number} duplicates id {entry_id!r}")
            seen_ids.add(entry_id)

            entry: dict[str, Any] = {"id": entry_id}
            for field in field_names:
                cell = values_by_header[field]
                if cell == "":
                    continue
                encoded = canonical_json(cell)
                estimated_durable_bytes += estimated_source_value_row_bytes(
                    entry_id, field, encoded
                )
                if estimated_durable_bytes > MAX_DATASET_DURABLE_BYTES:
                    raise DatasetSyntaxError(
                        "CSV import exceeds the "
                        f"{MAX_DATASET_DURABLE_BYTES}-byte estimated durable-row limit"
                    )
                entry[field] = cell
            entries.append(entry)
    except UnicodeDecodeError as error:
        raise DatasetSyntaxError("CSV files must use UTF-8 encoding") from error
    except csv.Error as error:
        raise DatasetSyntaxError("CSV file contains malformed quoting") from error

    if not entries:
        raise DatasetSyntaxError("CSV file contains no data rows")

    content_hash = dataset_content_hash(entries, field_names)
    return CsvDataset(
        path=source,
        version_id=f"csv_{content_hash}",
        content_hash=content_hash,
        byte_count=len(payload),
        cell_count=cell_count,
        estimated_durable_bytes=estimated_durable_bytes,
        field_names=field_names,
        entries=tuple(entries),
    )


def _read_bounded(handle: BinaryIO) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = handle.read(READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_CSV_BYTES:
            raise DatasetSyntaxError(f"CSV file exceeds the {MAX_CSV_BYTES}-byte import limit")
        chunks.append(chunk)
