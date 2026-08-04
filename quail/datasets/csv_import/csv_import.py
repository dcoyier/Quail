"""UTF-8 CSV loader for immutable dataset versions."""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator
from typing import Any, TextIO

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
    estimated_source_value_row_bytes,
)
from quail.datasets.models import CsvDataset


@dataclass(frozen=True, slots=True)
class CsvDatasetScan:
    """Bounded metadata and identity from a streaming CSV validation pass."""

    path: Path
    version_id: str
    content_hash: str
    byte_count: int
    row_count: int
    cell_count: int
    value_count: int
    estimated_durable_bytes: int
    field_names: tuple[str, ...]
    source_signature: tuple[int, int, int, int]


@dataclass(slots=True)
class _ScanCounters:
    row_count: int = 0
    cell_count: int = 0
    value_count: int = 0
    estimated_durable_bytes: int = 0


def scan_csv_dataset(path: str | Path) -> CsvDatasetScan:
    """Validate and identify one CSV without retaining its entry rows."""

    source = Path(os.path.abspath(Path(path).expanduser()))
    counters = _ScanCounters()
    try:
        with _open_csv(source) as (handle, signature):
            reader = csv.DictReader(handle, strict=True)
            headers, field_names = _validated_headers(reader)
            with tempfile.TemporaryDirectory(prefix="quail-csv-ids-") as temp_dir:
                ids = sqlite3.connect(Path(temp_dir) / "ids.sqlite")
                try:
                    ids.execute("PRAGMA journal_mode=OFF")
                    ids.execute("PRAGMA synchronous=OFF")
                    ids.execute("CREATE TABLE ids (id TEXT PRIMARY KEY) WITHOUT ROWID")

                    def unique_entries() -> Iterator[dict[str, Any]]:
                        for line_number, entry in _iter_entries(
                            reader, headers, field_names, counters
                        ):
                            try:
                                ids.execute("INSERT INTO ids(id) VALUES (?)", (entry["id"],))
                            except sqlite3.IntegrityError as error:
                                raise DatasetSyntaxError(
                                    f"CSV row {line_number} duplicates id {entry['id']!r}"
                                ) from error
                            yield entry

                    content_hash = dataset_content_hash(unique_entries(), field_names)
                finally:
                    ids.close()
    except UnicodeDecodeError as error:
        raise DatasetSyntaxError("CSV files must use UTF-8 encoding") from error
    except csv.Error as error:
        raise DatasetSyntaxError("CSV file contains malformed quoting") from error

    if counters.row_count == 0:
        raise DatasetSyntaxError("CSV file contains no data rows")
    if _source_signature(source) != signature:
        raise DatasetSyntaxError("CSV file changed while it was being read")

    return CsvDatasetScan(
        path=source,
        version_id=f"csv_{content_hash}",
        content_hash=content_hash,
        byte_count=signature[2],
        row_count=counters.row_count,
        cell_count=counters.cell_count,
        value_count=counters.value_count,
        estimated_durable_bytes=counters.estimated_durable_bytes,
        field_names=field_names,
        source_signature=signature,
    )


def iter_csv_entries(scan: CsvDatasetScan) -> Iterator[dict[str, Any]]:
    """Yield validated entries for an unchanged prior scan."""

    if _source_signature(scan.path) != scan.source_signature:
        raise DatasetSyntaxError("CSV file changed after its validation pass")
    counters = _ScanCounters()
    try:
        with _open_csv(scan.path) as (handle, signature):
            if signature != scan.source_signature:
                raise DatasetSyntaxError("CSV file changed after its validation pass")
            reader = csv.DictReader(handle, strict=True)
            headers, field_names = _validated_headers(reader)
            if field_names != scan.field_names:
                raise DatasetSyntaxError("CSV headers changed after the validation pass")
            for _line_number, entry in _iter_entries(reader, headers, field_names, counters):
                yield entry
    except UnicodeDecodeError as error:
        raise DatasetSyntaxError("CSV files must use UTF-8 encoding") from error
    except csv.Error as error:
        raise DatasetSyntaxError("CSV file contains malformed quoting") from error

    actual = (
        counters.row_count,
        counters.cell_count,
        counters.value_count,
        counters.estimated_durable_bytes,
    )
    expected = (
        scan.row_count,
        scan.cell_count,
        scan.value_count,
        scan.estimated_durable_bytes,
    )
    if actual != expected or _source_signature(scan.path) != scan.source_signature:
        raise DatasetSyntaxError("CSV file changed after its validation pass")


def load_csv_dataset(path: str | Path) -> CsvDataset:
    """Read one bounded UTF-8 CSV with a required unique ``id`` column."""

    scan = scan_csv_dataset(path)
    entries = tuple(iter_csv_entries(scan))
    return CsvDataset(
        path=scan.path,
        version_id=scan.version_id,
        content_hash=scan.content_hash,
        byte_count=scan.byte_count,
        cell_count=scan.cell_count,
        estimated_durable_bytes=scan.estimated_durable_bytes,
        field_names=scan.field_names,
        entries=entries,
    )


@contextmanager
def _open_csv(source: Path) -> Iterator[tuple[TextIO, tuple[int, int, int, int]]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as error:
        raise DatasetSyntaxError(f"CSV file does not exist: {source}") from error
    raw = os.fdopen(descriptor, "rb")
    try:
        metadata = os.fstat(raw.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise DatasetSyntaxError(f"CSV file does not exist: {source}")
        if metadata.st_size > MAX_CSV_BYTES:
            raise DatasetSyntaxError(f"CSV file exceeds the {MAX_CSV_BYTES}-byte import limit")
        signature = (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )
        with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
            yield text, signature
    finally:
        if not raw.closed:
            raw.close()


def _source_signature(source: Path) -> tuple[int, int, int, int]:
    with _open_csv(source) as (_handle, signature):
        return signature


def _validated_headers(
    reader: csv.DictReader[str],
) -> tuple[list[str], tuple[str, ...]]:
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
    return list(headers), field_names


def _iter_entries(
    reader: csv.DictReader[str],
    headers: list[str],
    field_names: tuple[str, ...],
    counters: _ScanCounters,
) -> Iterator[tuple[int, dict[str, Any]]]:
    normalized_headers = [header.strip() for header in headers]
    for line_number, raw_row in enumerate(reader, start=2):
        if counters.row_count >= MAX_CSV_ROWS:
            raise DatasetSyntaxError(f"CSV import exceeds the {MAX_CSV_ROWS}-row limit")
        if None in raw_row:
            raise DatasetSyntaxError(f"CSV row {line_number} has more values than headers")
        counters.cell_count += len(normalized_headers)
        if counters.cell_count > MAX_DATASET_CELLS:
            raise DatasetSyntaxError(f"CSV import exceeds the {MAX_DATASET_CELLS}-cell limit")

        values_by_header: dict[str, str] = {}
        for original, normalized in zip(headers, normalized_headers, strict=True):
            raw_value = raw_row.get(original)
            values_by_header[normalized] = "" if raw_value is None else str(raw_value).strip()

        entry_id = values_by_header["id"]
        if not entry_id:
            raise DatasetSyntaxError(f"CSV row {line_number} has a blank id")
        if len(entry_id.encode("utf-8")) > MAX_SOURCE_ENTRY_ID_BYTES:
            raise DatasetSyntaxError(
                f"CSV row {line_number} id cannot exceed "
                f"{MAX_SOURCE_ENTRY_ID_BYTES} UTF-8 bytes"
            )

        entry: dict[str, Any] = {"id": entry_id}
        for field in field_names:
            cell = values_by_header[field]
            if cell == "":
                continue
            encoded = canonical_json(cell)
            counters.estimated_durable_bytes += estimated_source_value_row_bytes(
                entry_id, field, encoded
            )
            if counters.estimated_durable_bytes > MAX_DATASET_DURABLE_BYTES:
                raise DatasetSyntaxError(
                    "CSV import exceeds the "
                    f"{MAX_DATASET_DURABLE_BYTES}-byte estimated durable-row limit"
                )
            counters.value_count += 1
            entry[field] = cell
        counters.row_count += 1
        yield line_number, entry
