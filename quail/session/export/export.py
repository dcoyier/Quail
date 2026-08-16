"""Write source columns plus one session overlay to a processable CSV."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quail.analysis.errors import QuailRuntimeError
from quail.datasets.catalog import source_entries, source_fields
from quail.datasets.db import CoreDb
from quail.datasets.hashing import canonical_json, decode_json
from quail.datasets.limits import (
    MAX_CSV_BYTES,
    MAX_CSV_ROWS,
    MAX_DATASET_CELLS,
    MAX_DATASET_DURABLE_BYTES,
    MAX_DATASET_FIELDS,
    MAX_SOURCE_FIELD_NAME_BYTES,
    estimated_source_value_row_bytes,
)
from quail.session.errors import SessionSyntaxError
from quail.session.overlay import analysis_fields, resolve_scope

_EXPORT_REPAIR = (
    "Narrow tagged columns or export a smaller dataset, then retry. "
    "The CSV must stay within the same import limits quail process uses."
)


@dataclass(frozen=True, slots=True)
class ExportCsvResult:
    """Host path and catalog of one session CSV snapshot."""

    path: Path
    session_id: str
    dataset_id: str
    dataset_version_id: str
    columns: tuple[str, ...]
    row_count: int


def export_session_csv(
    db: CoreDb,
    *,
    session_id: str,
    dataset_id: str,
    dest_dir: Path,
) -> ExportCsvResult:
    """Write source plus this session's analysis fields to dest_dir."""

    scope = resolve_scope(db, session_id, dataset_id)
    source = source_fields(
        db,
        scope.workspace_id,
        scope.dataset_id,
        scope.dataset_version_id,
    )
    analysis = analysis_fields(db, scope)
    analysis_names = [field.name for field in analysis]
    if "id" in analysis_names:
        raise SessionSyntaxError("Cannot export an analysis field named id")
    field_names = [field.name for field in source] + analysis_names
    if len(field_names) > MAX_DATASET_FIELDS:
        raise QuailRuntimeError(
            f"Export exceeds the {MAX_DATASET_FIELDS}-column field limit",
            repair_hint=_EXPORT_REPAIR,
        )
    for name in field_names:
        if len(name.encode("utf-8")) > MAX_SOURCE_FIELD_NAME_BYTES:
            raise QuailRuntimeError(
                f"CSV headers cannot exceed {MAX_SOURCE_FIELD_NAME_BYTES} UTF-8 bytes",
                repair_hint=_EXPORT_REPAIR,
            )
    columns = ("id", *field_names)
    entries = source_entries(
        db,
        scope.workspace_id,
        scope.dataset_id,
        scope.dataset_version_id,
    )
    if len(entries) > MAX_CSV_ROWS:
        raise QuailRuntimeError(
            f"Export exceeds the {MAX_CSV_ROWS}-row limit",
            repair_hint=_EXPORT_REPAIR,
        )
    cell_count = len(entries) * len(columns)
    if cell_count > MAX_DATASET_CELLS:
        raise QuailRuntimeError(
            f"Export exceeds the {MAX_DATASET_CELLS}-cell limit",
            repair_hint=_EXPORT_REPAIR,
        )
    if not entries:
        raise SessionSyntaxError("Dataset version has no entries")

    source_by_entry = _values_by_entry(
        db,
        """
        SELECT entry_id, field_name, value_json
        FROM quail_source_values
        WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
        """,
        (scope.workspace_id, scope.dataset_id, scope.dataset_version_id),
    )
    analysis_by_entry = _values_by_entry(
        db,
        """
        SELECT entry_id, field_name, value_json
        FROM quail_analysis_values
        WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
          AND dataset_version_id = ?
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
        ),
    )

    dest_dir = Path(dest_dir).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_safe_filename_part(scope.dataset_id)}.{_safe_filename_part(scope.session_id)}.csv"
    path = dest_dir / filename
    if path.parent != dest_dir:
        raise SessionSyntaxError("Export path escaped the destination directory")

    estimated_durable_bytes = 0
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(columns)
            for entry in entries:
                row = [entry.id]
                source_cells = source_by_entry.get(entry.id, {})
                analysis_cells = analysis_by_entry.get(entry.id, {})
                for field in source:
                    cell = _csv_cell(source_cells.get(field.name))
                    row.append(cell)
                    if cell:
                        estimated_durable_bytes += estimated_source_value_row_bytes(
                            entry.id,
                            field.name,
                            canonical_json(cell),
                        )
                for analysis_field in analysis:
                    cell = _csv_cell(analysis_cells.get(analysis_field.name))
                    row.append(cell)
                    if cell:
                        estimated_durable_bytes += estimated_source_value_row_bytes(
                            entry.id,
                            analysis_field.name,
                            canonical_json(cell),
                        )
                if estimated_durable_bytes > MAX_DATASET_DURABLE_BYTES:
                    raise QuailRuntimeError(
                        "Export exceeds the "
                        f"{MAX_DATASET_DURABLE_BYTES}-byte estimated durable-row limit",
                        repair_hint=_EXPORT_REPAIR,
                    )
                writer.writerow(row)
        byte_count = tmp_path.stat().st_size
        if byte_count > MAX_CSV_BYTES:
            raise QuailRuntimeError(
                f"Export exceeds the {MAX_CSV_BYTES}-byte import limit",
                repair_hint=_EXPORT_REPAIR,
            )
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return ExportCsvResult(
        path=path,
        session_id=scope.session_id,
        dataset_id=scope.dataset_id,
        dataset_version_id=scope.dataset_version_id,
        columns=columns,
        row_count=len(entries),
    )


def _values_by_entry(
    db: CoreDb,
    sql: str,
    params: tuple[object, ...],
) -> dict[str, dict[str, Any]]:
    by_entry: dict[str, dict[str, Any]] = {}
    rows = db.connection.execute(sql, params).fetchall()
    for row in rows:
        entry_id = str(row[0])
        field_name = str(row[1])
        by_entry.setdefault(entry_id, {})[field_name] = decode_json(str(row[2]))
    return by_entry


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return canonical_json(value)


def _safe_filename_part(value: str) -> str:
    allowed = all(ch.isalnum() or ch in "._-" for ch in value)
    if not allowed or not value or value in {".", ".."}:
        raise SessionSyntaxError("Export filename cannot be built from this id")
    return value
