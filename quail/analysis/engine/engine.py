"""Host QueryEngine: evaluate plans against catalog + in-memory overlay."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from quail.analysis.entry import Entry, make_entry
from quail.analysis.errors import (
    QuailFieldError,
    QuailRuntimeError,
    QuailScopeError,
    QuailSyntaxError,
)
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import GroupExpr
from quail.analysis.literals import literal_as_plain
from quail.analysis.operations import Operation
from quail.analysis.planner import (
    CountPlan,
    CreateFieldPlan,
    RetrievePlan,
    TagPlan,
    UntagPlan,
)
from quail.analysis.predicate import Predicate
from quail.analysis.ranking import Ranking
from quail.analysis.regex_engine import compile_regex
from quail.analysis.search_text import (
    entry_from_record,
    group_expr_from_record,
    lexical_document_query,
    text_segments,
)
from quail.analysis.unit import Unit
from quail.datasets.catalog import source_entries, source_values
from quail.datasets.db import CoreDb
from quail.search import LexicalService, SimilarityService
from quail.session.models import FieldCreate, Scope, ValueDelete, ValueWrite
from quail.session.overlay import analysis_fields, analysis_values, catalog_fields

_LEXICAL_NOT_CONFIGURED_HINT = (
    "Set core.search_database, re-run quail process, then retry the whole exec."
)
_SEMANTIC_NOT_CONFIGURED_HINT = (
    "Set core.search_database, [providers.*], and [datasets.embedding], "
    "re-run quail process, then retry the whole exec."
)


class QueryEngine:
    """Evaluate retrieve/count/mutations for one scope; stage overlay in memory."""

    def __init__(
        self,
        db: CoreDb,
        scope: Scope,
        *,
        similarity: SimilarityService | None = None,
        lexical: LexicalService | None = None,
    ) -> None:
        self._db = db
        self._scope = scope
        self._similarity = similarity
        self._lexical = lexical
        self._mutations: list[FieldCreate | ValueWrite | ValueDelete] = []
        self._created_fields: dict[str, Field] = {}
        # (field_name, entry_id) -> value | _ABSENT sentinel for deletes in overlay
        self._value_overlay: dict[tuple[str, str], Any] = {}
        self._absent = object()
        # Structural Expression key -> entry_id -> Lexical/Semantic score (one exec).
        self._search_scores: dict[str, dict[str, float | None]] = {}
        # Execution snapshot: ordered ids, positions, and per-field value maps.
        self._ordered_entry_ids: list[str] | None = None
        self._entry_positions: dict[str, int] | None = None
        self._field_value_maps: dict[str, dict[str, Any]] = {}
        self._catalog_cache: list[Any] | None = None

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def mutations(self) -> tuple[FieldCreate | ValueWrite | ValueDelete, ...]:
        return tuple(self._mutations)

    def retrieve(self, plan: RetrievePlan) -> list[Any]:
        if isinstance(plan.unit, Unit) and plan.unit.scope == "fields":
            fields = self._evaluate_field_group(plan.group)
            return self._apply_limit(fields, plan.limit, plan.order)

        search_scores = self._search_scores
        entry_ids = self._evaluate_entry_group(plan.group, search_scores=search_scores)
        if isinstance(plan.unit, Unit) and plan.unit.scope == "values":
            assert plan.unit.field is not None
            # Deduplicate the full group first; limit/order apply to distinct values.
            values = self._distinct_present_values(plan.unit.field, entry_ids)
            return self._apply_limit(values, plan.limit, plan.order)

        if (
            isinstance(plan.unit, Unit)
            and plan.unit.scope == "entries"
            and plan.unit.field is not None
        ):
            unit_field = plan.unit.field
            entry_ids = [
                entry_id
                for entry_id in entry_ids
                if self._read_field_value(unit_field, entry_id) is not None
            ]
        entry_ids = self._apply_ranking(entry_ids, plan.ranking, search_scores=search_scores)
        entry_ids = self._apply_limit(entry_ids, plan.limit, plan.order)

        if isinstance(plan.unit, Expression):
            self._ensure_search_expression_scores([plan.unit], entry_ids, search_scores)
            return [
                self._eval_expression(plan.unit, entry_id, search_scores=search_scores)
                for entry_id in entry_ids
            ]

        assert isinstance(plan.unit, Unit)
        if plan.unit.scope == "entries":
            if plan.unit.field is None:
                return [self._make_entry(entry_id) for entry_id in entry_ids]
            return [self._read_field_value(plan.unit.field, entry_id) for entry_id in entry_ids]
        raise QuailSyntaxError(f"Unsupported unit scope: {plan.unit.scope}")

    def count(self, plan: CountPlan) -> int:
        if isinstance(plan.unit, Unit) and plan.unit.scope == "fields":
            return len(self._evaluate_field_group(plan.group))
        search_scores = self._search_scores
        entry_ids = self._evaluate_entry_group(plan.group, search_scores=search_scores)
        if isinstance(plan.unit, Expression):
            return len(entry_ids)
        assert isinstance(plan.unit, Unit)
        if plan.unit.scope == "entries":
            if plan.unit.field is None:
                return len(entry_ids)
            return sum(
                1
                for entry_id in entry_ids
                if self._read_field_value(plan.unit.field, entry_id) is not None
            )
        if plan.unit.scope == "values":
            assert plan.unit.field is not None
            return len(self._distinct_present_values(plan.unit.field, entry_ids))
        raise QuailSyntaxError(f"Unsupported unit scope: {plan.unit.scope}")

    def create_field(self, plan: CreateFieldPlan) -> Field:
        name = plan.field.name
        if any(field.name == name and field.kind == "source" for field in self._catalog()):
            raise QuailFieldError(f"Cannot create analysis field over source name: {name}")
        if name in self._created_fields or any(
            field.name == name and field.kind == "analysis" for field in self._catalog()
        ):
            return Field(name, kind="analysis")
        self._created_fields[name] = Field(name, kind="analysis")
        self._mutations.append(FieldCreate(name))
        self._catalog_cache = None
        self._field_value_maps.pop(name, None)
        return self._created_fields[name]

    def tag(self, plan: TagPlan) -> None:
        field = self._require_analysis_field(plan.field)
        field_name = field.name
        mutated = False
        for entry_id in self._tag_entry_ids(plan.group):
            self._value_overlay[(field_name, entry_id)] = plan.value
            self._mutations.append(ValueWrite(field_name, entry_id, plan.value))
            mutated = True
        if mutated:
            self._search_scores.clear()

    def untag(self, plan: UntagPlan) -> None:
        field = self._require_analysis_field(plan.field)
        field_name = field.name
        mutated = False
        for entry_id in self._tag_entry_ids(plan.group):
            current = self._read_field_value(field, entry_id)
            if current is None:
                continue
            if plan.value is not None and current != plan.value:
                continue
            self._value_overlay[(field_name, entry_id)] = self._absent
            self._mutations.append(ValueDelete(field_name, entry_id, plan.value))
            mutated = True
        if mutated:
            self._search_scores.clear()

    def entry_value(
        self,
        entry: Entry,
        field: Field | str,
        default: Any = None,
    ) -> Any:
        self._require_entry_in_scope(entry)
        if not isinstance(field, Field | str):
            raise QuailSyntaxError("field must be a Field or non-empty string")
        if isinstance(field, str) and not field:
            raise QuailSyntaxError("field must be a Field or non-empty string")
        value = self._read_field_value(field, entry.id)
        return default if value is None else value

    def entry_fields(self, entry: Entry) -> list[Field]:
        self._require_entry_in_scope(entry)
        present: list[Field] = []
        for field in self._catalog():
            if self._read_field_value(Field(field.name, kind=field.kind), entry.id) is not None:
                present.append(Field(field.name, kind=field.kind))
        return present

    def _catalog(self) -> list[Any]:
        from quail.session.models import CatalogField

        if self._catalog_cache is not None:
            return self._catalog_cache
        result: list[CatalogField] = list(catalog_fields(self._db, self._scope))
        seen = {field.name for field in result}
        for name in self._created_fields:
            if name not in seen:
                result.append(CatalogField(name=name, kind="analysis", position=len(result)))
        self._catalog_cache = result
        return result

    def _require_entry_in_scope(self, entry: Entry, *, operation_kind: str | None = None) -> None:
        """Fail when a stamped Entry handle belongs to another dataset/version."""

        label = operation_kind or "Entry"
        if entry.dataset_id and entry.dataset_id != self._scope.dataset_id:
            raise QuailScopeError(f"{label} does not belong to this dataset")
        if entry.dataset_version_id and entry.dataset_version_id != self._scope.dataset_version_id:
            raise QuailScopeError(f"{label} does not belong to this dataset version")

    def _make_entry(self, entry_id: str) -> Entry:
        return make_entry(
            entry_id,
            dataset_id=self._scope.dataset_id,
            dataset_version_id=self._scope.dataset_version_id,
            dataset=self._scope.dataset_id,
        )

    def _tag_entry_ids(self, group: GroupExpr | tuple[Entry, ...]) -> list[str]:
        if isinstance(group, tuple):
            ids: list[str] = []
            for entry in group:
                self._require_entry_in_scope(entry)
                ids.append(entry.id)
            return ids
        return self._evaluate_entry_group(group, search_scores=self._search_scores)

    def resolve_field(self, field: Field | str) -> Field:
        """Resolve a Field or name against the catalog; enforce kind when set."""

        if isinstance(field, Field):
            name = field.name
            requested_kind = field.kind
        elif isinstance(field, str) and field:
            name = field
            requested_kind = None
        else:
            raise QuailSyntaxError("field must be a Field or non-empty string")

        same_name = None
        for catalog_field in self._catalog():
            if catalog_field.name != name:
                continue
            same_name = catalog_field
            if requested_kind is None or catalog_field.kind == requested_kind:
                return Field(catalog_field.name, kind=catalog_field.kind)
        if requested_kind is not None and same_name is not None:
            raise QuailFieldError(
                f"Field {name!r} is registered as {same_name.kind}, not {requested_kind}; "
                f"use Field({name!r}, kind={same_name.kind!r}) or omit kind"
            )
        if requested_kind is None:
            raise QuailFieldError(f"Unknown field: {name}")
        raise QuailFieldError(f"Unknown {requested_kind} field: {name}")

    def check_bound_field_kind(self, field: Field) -> None:
        """At bind restore/commit: enforce explicit kind vs catalog; skip unknown names."""

        if not isinstance(field, Field):
            raise QuailSyntaxError("check_bound_field_kind requires a Field")
        if field.kind is None:
            return
        for catalog_field in self._catalog():
            if catalog_field.name != field.name:
                continue
            if catalog_field.kind != field.kind:
                raise QuailFieldError(
                    f"Field {field.name!r} is registered as {catalog_field.kind}, "
                    f"not {field.kind}; "
                    f"use Field({field.name!r}, kind={catalog_field.kind!r}) or omit kind"
                )
            return

    def _require_analysis_field(self, field: Field | str) -> Field:
        resolved = self.resolve_field(field)
        if resolved.kind != "analysis":
            raise QuailFieldError(f"Cannot tag source field: {resolved.name}")
        return resolved

    def _all_entry_ids(self) -> list[str]:
        if self._ordered_entry_ids is None:
            self._ordered_entry_ids = [
                entry.id
                for entry in source_entries(
                    self._db,
                    self._scope.workspace_id,
                    self._scope.dataset_id,
                    self._scope.dataset_version_id,
                )
            ]
            self._entry_positions = {
                entry_id: index for index, entry_id in enumerate(self._ordered_entry_ids)
            }
        return self._ordered_entry_ids

    def _entry_position(self, entry_id: str) -> int:
        self._all_entry_ids()
        assert self._entry_positions is not None
        return self._entry_positions.get(entry_id, len(self._entry_positions))

    def _evaluate_entry_group(
        self,
        group: GroupExpr,
        *,
        search_scores: dict[str, dict[str, float | None]],
    ) -> list[str]:
        if group.scope != "entries":
            raise QuailSyntaxError("Expected an entry-scoped group")
        if group.name == "G0":
            return self._all_entry_ids()
        if group.members is not None:
            ids: list[str] = []
            for member in group.members:
                self._require_entry_in_scope(member)
                ids.append(member.id)
            return ids
        if group.predicate is not None:
            candidate_ids = self._all_entry_ids()
            self._ensure_search_expression_scores(
                _search_predicate_expressions(group.predicate),
                candidate_ids,
                search_scores,
            )
            return [
                entry_id
                for entry_id in candidate_ids
                if self._eval_predicate(group.predicate, entry_id, search_scores=search_scores)
            ]
        if group.operator == "and":
            assert group.left is not None and group.right is not None
            right_ids = set(self._evaluate_entry_group(group.right, search_scores=search_scores))
            return [
                entry_id
                for entry_id in self._evaluate_entry_group(group.left, search_scores=search_scores)
                if entry_id in right_ids
            ]
        if group.operator == "or":
            assert group.left is not None and group.right is not None
            seen: set[str] = set()
            result: list[str] = []
            for entry_id in self._evaluate_entry_group(
                group.left, search_scores=search_scores
            ) + self._evaluate_entry_group(group.right, search_scores=search_scores):
                if entry_id not in seen:
                    seen.add(entry_id)
                    result.append(entry_id)
            return result
        if group.operator == "not":
            assert group.left is not None
            excluded = set(self._evaluate_entry_group(group.left, search_scores=search_scores))
            return [entry_id for entry_id in self._all_entry_ids() if entry_id not in excluded]
        raise QuailSyntaxError("Unsupported entry group form")

    def _evaluate_field_group(self, group: GroupExpr) -> list[Field]:
        if group.scope != "fields":
            raise QuailSyntaxError("Expected a field-scoped group")
        catalog = self._catalog()
        if group.name == "G1":
            return [Field(field.name, kind=field.kind) for field in catalog]
        if group.members is not None:
            return [self.resolve_field(field) for field in group.members]
        if group.operator == "and":
            assert group.left is not None and group.right is not None
            right = {field.name for field in self._evaluate_field_group(group.right)}
            return [
                field for field in self._evaluate_field_group(group.left) if field.name in right
            ]
        if group.operator == "or":
            assert group.left is not None and group.right is not None
            seen: set[str] = set()
            result: list[Field] = []
            for field in self._evaluate_field_group(group.left) + self._evaluate_field_group(
                group.right
            ):
                if field.name not in seen:
                    seen.add(field.name)
                    result.append(field)
            return result
        if group.operator == "not":
            assert group.left is not None
            excluded = {field.name for field in self._evaluate_field_group(group.left)}
            return [
                Field(field.name, kind=field.kind)
                for field in catalog
                if field.name not in excluded
            ]
        raise QuailSyntaxError("Unsupported field group form")

    def _eval_predicate(
        self,
        predicate: Predicate,
        entry_id: str,
        *,
        search_scores: dict[str, dict[str, float | None]],
    ) -> bool:
        if predicate.operator == "and":
            return self._eval_predicate(
                predicate.left, entry_id, search_scores=search_scores
            ) and self._eval_predicate(predicate.right, entry_id, search_scores=search_scores)
        if predicate.operator == "or":
            return self._eval_predicate(
                predicate.left, entry_id, search_scores=search_scores
            ) or self._eval_predicate(predicate.right, entry_id, search_scores=search_scores)
        if predicate.operator == "not":
            return not self._eval_predicate(predicate.left, entry_id, search_scores=search_scores)

        left = self._eval_expression(predicate.left, entry_id, search_scores=search_scores)
        right = predicate.right
        if isinstance(right, Expression):
            right = self._eval_expression(right, entry_id, search_scores=search_scores)
        else:
            right = literal_as_plain(right)

        if predicate.operator == "==":
            return left == right
        if predicate.operator == "!=":
            return left != right
        if left is None or right is None:
            return False
        if not isinstance(left, int | float) or not isinstance(right, int | float):
            raise QuailRuntimeError("Numeric comparison requires numeric operands")
        if predicate.operator == "<":
            return left < right
        if predicate.operator == "<=":
            return left <= right
        if predicate.operator == ">":
            return left > right
        if predicate.operator == ">=":
            return left >= right
        raise QuailSyntaxError(f"Unsupported predicate operator: {predicate.operator}")

    def _eval_expression(
        self,
        expression: Expression,
        entry_id: str,
        *,
        search_scores: dict[str, dict[str, float | None]] | None = None,
    ) -> Any:
        score_key = _expression_score_key(expression)
        if (
            search_scores is not None
            and expression.operations
            and expression.operations[-1].kind in ("Lexical", "Semantic")
            and score_key in search_scores
            and entry_id in search_scores[score_key]
        ):
            return search_scores[score_key].get(entry_id)
        value: Any = self._read_field_value(expression.root, entry_id)
        for index, operation in enumerate(expression.operations):
            source_field = None
            if operation.kind == "Lexical":
                source_field = self._warmed_lexical_source_field(
                    expression, prefix_count=index
                )
            value = self._apply_operation(
                operation,
                value,
                root=expression.root,
                search_scores=search_scores,
                source_field=source_field,
            )
        return value

    def _warmed_lexical_source_field(
        self, expression: Expression, *, prefix_count: int
    ) -> str | None:
        """Source field name for warm reuse when Lexical has no transforming prefixes."""

        if prefix_count != 0:
            return None
        return self._bare_source_search_field(expression)

    def _apply_operation(
        self,
        operation: Operation,
        value: Any,
        *,
        root: Field | None = None,
        search_scores: dict[str, dict[str, float | None]] | None = None,
        source_field: str | None = None,
    ) -> Any:
        kind = operation.kind
        if kind == "Value":
            return value
        if kind == "AsText":
            return "" if value is None else str(value)
        if kind == "AsNumber":
            if value is None:
                return None
            if isinstance(value, bool):
                raise QuailRuntimeError("AsNumber cannot convert bool")
            if isinstance(value, int | float):
                number = float(value)
            elif isinstance(value, str):
                try:
                    number = float(value.strip())
                except ValueError as error:
                    raise QuailRuntimeError("AsNumber could not parse text") from error
            else:
                raise QuailRuntimeError("AsNumber requires a number or numeric string")
            if not math.isfinite(number):
                raise QuailRuntimeError("AsNumber requires a finite number")
            return number
        if kind in ("RegexSearch", "RegexFindAll", "RegexSub"):
            return self._apply_regex(operation, value)
        if kind == "Slice":
            start = operation.params["start"]
            end = operation.params["end"]
            if value is None:
                return None
            if isinstance(value, str):
                return value[start:end]
            if isinstance(value, list):
                return value[start:end]
            raise QuailRuntimeError("Slice requires text or list[text]")
        if kind == "Length":
            if value is None:
                return 0
            if isinstance(value, str | list):
                return len(value)
            raise QuailRuntimeError("Length requires text, list, or None")
        if kind == "Lexical":
            if self._lexical is None:
                raise QuailRuntimeError(
                    "Lexical search is not configured",
                    repair_hint=_LEXICAL_NOT_CONFIGURED_HINT,
                )
            if root is None:
                raise QuailRuntimeError("Lexical requires an expression root field")
            return self._lexical.lexical_score(
                workspace_id=self._scope.workspace_id,
                dataset_id=self._scope.dataset_id,
                version_id=self._scope.dataset_version_id,
                corpus=value,
                query_record=self._resolved_search_query(
                    dict(operation.params["query"]),
                    root=root,
                    operation_kind="Lexical",
                    search_scores=search_scores,
                ),
                input_aggregation=operation.params.get("input_aggregation"),
                target_aggregation=operation.params.get("target_aggregation"),
                source_field=source_field,
            )
        if kind == "Semantic":
            if self._similarity is None:
                raise QuailRuntimeError(
                    "Semantic search is not configured",
                    repair_hint=_SEMANTIC_NOT_CONFIGURED_HINT,
                )
            if root is None:
                raise QuailRuntimeError("Semantic requires an expression root field")
            return self._similarity.semantic_score(
                workspace_id=self._scope.workspace_id,
                dataset_id=self._scope.dataset_id,
                version_id=self._scope.dataset_version_id,
                corpus=value,
                query_record=self._resolved_search_query(
                    dict(operation.params["query"]),
                    root=root,
                    operation_kind="Semantic",
                    search_scores=search_scores,
                ),
                input_aggregation=operation.params.get("input_aggregation"),
                target_aggregation=operation.params.get("target_aggregation"),
            )
        raise QuailSyntaxError(f"Unsupported operation: {kind}")

    def _resolved_search_query(
        self,
        query_record: dict[str, Any],
        *,
        root: Field,
        operation_kind: str,
        search_scores: dict[str, dict[str, float | None]] | None = None,
    ) -> dict[str, Any]:
        texts = self._resolve_search_targets(
            query_record,
            root=root,
            operation_kind=operation_kind,
            search_scores=search_scores,
        )
        if len(texts) == 1:
            return {"kind": "LiteralText", "text": texts[0]}
        return {"kind": "LiteralTextList", "texts": texts}

    def _resolve_search_targets(
        self,
        query_record: dict[str, Any],
        *,
        root: Field,
        operation_kind: str,
        search_scores: dict[str, dict[str, float | None]] | None = None,
    ) -> list[str]:
        """Expand Lexical/Semantic query records into ordered non-empty target strings."""

        scores = search_scores if search_scores is not None else {}
        kind = query_record.get("kind")
        if kind == "LiteralText":
            targets = text_segments(query_record.get("text"), operation_kind=operation_kind)
        elif kind == "LiteralTextList":
            texts = query_record.get("texts")
            if not isinstance(texts, Sequence) or isinstance(texts, str | bytes):
                raise QuailRuntimeError(f"{operation_kind} LiteralTextList query is malformed")
            targets = tuple(
                segment
                for value in texts
                for segment in text_segments(value, operation_kind=operation_kind)
            )
        elif kind == "EntryGroup":
            group = group_expr_from_record(query_record.get("group"))
            if group.scope != "entries":
                raise QuailRuntimeError(f"{operation_kind} target groups must be entry-scoped")
            entry_ids = self._evaluate_entry_group(group, search_scores=scores)
            targets = tuple(
                segment
                for entry_id in entry_ids
                for segment in text_segments(
                    self._read_field_value(root, entry_id),
                    operation_kind=operation_kind,
                )
            )
        elif kind == "EntryList":
            entries_record = query_record.get("entries")
            if not isinstance(entries_record, Sequence) or isinstance(entries_record, str | bytes):
                raise QuailRuntimeError(f"{operation_kind} EntryList query is malformed")
            listed_ids: list[str] = []
            for item in entries_record:
                entry = entry_from_record(item)
                self._validate_search_entry(entry, operation_kind=operation_kind)
                listed_ids.append(entry.id)
            targets = tuple(
                segment
                for entry_id in listed_ids
                for segment in text_segments(
                    self._read_field_value(root, entry_id),
                    operation_kind=operation_kind,
                )
            )
        else:
            raise QuailRuntimeError(f"Unsupported {operation_kind} query")

        if operation_kind == "Lexical" and kind in {"EntryGroup", "EntryList"}:
            targets = tuple(lexical_document_query(target) for target in targets)
        targets = tuple(target for target in targets if target)
        if not targets:
            raise QuailRuntimeError(f"{operation_kind} query has no non-empty target text")
        return list(targets)

    def _validate_search_entry(self, entry: Entry, *, operation_kind: str) -> None:
        self._require_entry_in_scope(entry, operation_kind=f"{operation_kind} Entry target")

    def _apply_regex(self, operation: Operation, value: Any) -> Any:
        pattern = str(operation.params["pattern"])
        flags = int(operation.params.get("flags", 0))
        compiled = compile_regex(pattern, flags)
        if operation.kind == "RegexSearch":
            if value is None:
                return None
            if not isinstance(value, str):
                raise QuailRuntimeError("RegexSearch requires text; use AsText() first")
            return compiled.search(value)
        if operation.kind == "RegexFindAll":
            if value is None:
                return []
            if not isinstance(value, str):
                raise QuailRuntimeError("RegexFindAll requires text; use AsText() first")
            return compiled.find_all(value)
        # RegexSub — literal replacement only (no backrefs).
        replacement = str(operation.params["replacement"])
        if value is None:
            return None
        if isinstance(value, str):
            return compiled.sub_literal(value, replacement)
        if isinstance(value, list):
            return [
                compiled.sub_literal(item, replacement) if isinstance(item, str) else item
                for item in value
            ]
        raise QuailRuntimeError("RegexSub requires text or list[text]; use AsText() first")

    def _read_field_value(self, field: Field | str, entry_id: str) -> Any:
        resolved = self.resolve_field(field)
        field_name = resolved.name
        key = (field_name, entry_id)
        if key in self._value_overlay:
            value = self._value_overlay[key]
            return None if value is self._absent else value

        self._ensure_field_snapshot(resolved)
        return self._field_value_maps[field_name].get(entry_id)

    def _ensure_field_snapshot(self, field: Field) -> None:
        """Bulk-load one field into the exec snapshot (lazy, once per field)."""

        field_name = field.name
        if field_name in self._field_value_maps:
            return
        ordered_ids = self._all_entry_ids()
        kind = field.kind
        if kind == "source":
            values = source_values(
                self._db,
                self._scope.workspace_id,
                self._scope.dataset_id,
                self._scope.dataset_version_id,
                field_name,
                entry_ids=None,
            )
        elif kind == "analysis":
            if field_name in self._created_fields and not any(
                catalog.name == field_name for catalog in analysis_fields(self._db, self._scope)
            ):
                values = [None] * len(ordered_ids)
            else:
                values = analysis_values(
                    self._db,
                    self._scope,
                    field_name,
                    entry_ids=None,
                )
        else:
            raise QuailFieldError(f"Unknown field: {field_name}")
        self._field_value_maps[field_name] = {
            entry_id: value for entry_id, value in zip(ordered_ids, values, strict=True)
        }

    def _apply_ranking(
        self,
        entry_ids: list[str],
        ranking: Ranking,
        *,
        search_scores: dict[str, dict[str, float | None]],
    ) -> list[str]:
        if (
            ranking.expression is None
            and ranking.left is None
            and ranking.operator is None
            and ranking.right is None
        ):
            return entry_ids
        self._ensure_search_expression_scores(
            _search_ranking_expressions(ranking),
            entry_ids,
            search_scores,
        )
        scored = [
            (self._score_ranking(ranking, entry_id, search_scores), entry_id)
            for entry_id in entry_ids
        ]
        scored.sort(key=lambda item: (-item[0], self._entry_position(item[1])))
        return [entry_id for _, entry_id in scored]

    def _ensure_search_expression_scores(
        self,
        expressions: list[Expression],
        entry_ids: list[str],
        search_scores: dict[str, dict[str, float | None]],
    ) -> None:
        """Batch-score Lexical/Semantic-terminal expressions; gap-fill missing entry ids."""

        if not entry_ids:
            return
        for expression in expressions:
            if not _is_search_terminal_expression(expression):
                continue
            key = _expression_score_key(expression)
            cached = search_scores.get(key)
            missing_ids = (
                list(entry_ids)
                if cached is None
                else [entry_id for entry_id in entry_ids if entry_id not in cached]
            )
            if not missing_ids:
                continue
            operation = expression.operations[-1]
            prefix_ops = expression.operations[:-1]
            source_field = self._bare_source_search_field(expression)
            all_entries = missing_ids == self._all_entry_ids()
            query_record = self._resolved_search_query(
                dict(operation.params["query"]),
                root=expression.root,
                operation_kind=operation.kind,
                search_scores=search_scores,
            )
            if operation.kind == "Semantic":
                if self._similarity is None:
                    raise QuailRuntimeError(
                        "Semantic search is not configured",
                        repair_hint=_SEMANTIC_NOT_CONFIGURED_HINT,
                    )
                scored = None
                if source_field is not None:
                    scored = self._similarity.semantic_scores_for_source_entries(
                        workspace_id=self._scope.workspace_id,
                        dataset_id=self._scope.dataset_id,
                        version_id=self._scope.dataset_version_id,
                        entry_ids=missing_ids,
                        source_field=source_field,
                        all_entries=all_entries,
                        query_record=query_record,
                        input_aggregation=operation.params.get("input_aggregation"),
                        target_aggregation=operation.params.get("target_aggregation"),
                    )
                if scored is None:
                    scored = self._similarity.semantic_scores_for_entries(
                        workspace_id=self._scope.workspace_id,
                        dataset_id=self._scope.dataset_id,
                        version_id=self._scope.dataset_version_id,
                        corpus_by_entry=self._corpus_by_entry(
                            expression,
                            prefix_ops,
                            missing_ids,
                            search_scores,
                        ),
                        query_record=query_record,
                        input_aggregation=operation.params.get("input_aggregation"),
                        target_aggregation=operation.params.get("target_aggregation"),
                    )
            else:
                if self._lexical is None:
                    raise QuailRuntimeError(
                        "Lexical search is not configured",
                        repair_hint=_LEXICAL_NOT_CONFIGURED_HINT,
                    )
                scored = None
                if source_field is not None:
                    scored = self._lexical.lexical_scores_for_source_entries(
                        workspace_id=self._scope.workspace_id,
                        dataset_id=self._scope.dataset_id,
                        version_id=self._scope.dataset_version_id,
                        entry_ids=missing_ids,
                        source_field=source_field,
                        all_entries=all_entries,
                        query_record=query_record,
                        input_aggregation=operation.params.get("input_aggregation"),
                        target_aggregation=operation.params.get("target_aggregation"),
                    )
                if scored is None:
                    scored = dict(
                        self._lexical.lexical_scores_for_entries(
                            workspace_id=self._scope.workspace_id,
                            dataset_id=self._scope.dataset_id,
                            version_id=self._scope.dataset_version_id,
                            corpus_by_entry=self._corpus_by_entry(
                                expression,
                                prefix_ops,
                                missing_ids,
                                search_scores,
                            ),
                            query_record=query_record,
                            input_aggregation=operation.params.get("input_aggregation"),
                            target_aggregation=operation.params.get("target_aggregation"),
                            source_field=source_field,
                        )
                    )
            if cached is None:
                search_scores[key] = dict(scored)
            else:
                cached.update(scored)

    def _bare_source_search_field(self, expression: Expression) -> str | None:
        """Source field name when the search op has no transforming prefixes."""

        if len(expression.operations) != 1:
            return None
        resolved = self.resolve_field(expression.root)
        if resolved.kind != "source":
            return None
        return resolved.name

    def _corpus_by_entry(
        self,
        expression: Expression,
        prefix_ops: tuple[Operation, ...],
        entry_ids: list[str],
        search_scores: dict[str, dict[str, float | None]],
    ) -> dict[str, Any]:
        """Materialize corpus values only for dynamic or unwarmed search scoring."""

        corpus_by_entry: dict[str, Any] = {}
        for entry_id in entry_ids:
            if prefix_ops:
                corpus_by_entry[entry_id] = self._eval_expression(
                    Expression(expression.root, *prefix_ops),
                    entry_id,
                    search_scores=search_scores,
                )
            else:
                corpus_by_entry[entry_id] = self._read_field_value(
                    expression.root, entry_id
                )
        return corpus_by_entry

    def _score_ranking(
        self,
        ranking: Ranking,
        entry_id: str,
        search_scores: dict[str, dict[str, float | None]] | None = None,
    ) -> float:
        if ranking.expression is not None:
            expression = ranking.expression
            score_key = _expression_score_key(expression)
            if (
                search_scores is not None
                and expression.operations
                and expression.operations[-1].kind in ("Lexical", "Semantic")
                and score_key in search_scores
                and entry_id in search_scores[score_key]
            ):
                value = search_scores[score_key].get(entry_id)
            else:
                value = self._eval_expression(expression, entry_id, search_scores=search_scores)
            if value is None:
                return float("-inf")
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise QuailRuntimeError("Ranking expression must produce a number")
            number = float(value)
            if not math.isfinite(number):
                return float("-inf")
            return number
        if (
            ranking.operator == "+"
            and ranking.left is not None
            and isinstance(ranking.right, Ranking)
        ):
            return self._score_ranking(ranking.left, entry_id, search_scores) + self._score_ranking(
                ranking.right, entry_id, search_scores
            )
        if ranking.operator == "*" and ranking.left is not None:
            weight = float(ranking.right)
            return self._score_ranking(ranking.left, entry_id, search_scores) * weight
        raise QuailSyntaxError("Unsupported ranking form")

    def _apply_limit(self, items: list[Any], limit: int, order: str) -> list[Any]:
        if not items:
            return []
        if order == "top":
            return items[:limit]
        if order == "bottom":
            # Reverse of the ranked/processing sequence (worst-first), then limit.
            reversed_items = list(reversed(items))
            return reversed_items[:limit]
        # middle
        if len(items) <= limit:
            return list(items)
        start = max(0, (len(items) - limit) // 2)
        return items[start : start + limit]

    def _distinct_present_values(self, field: Field, entry_ids: list[str]) -> list[Any]:
        """First-seen distinct present values over entry_ids (None excluded)."""

        seen: set[str] = set()
        values: list[Any] = []
        for entry_id in entry_ids:
            value = self._read_field_value(field, entry_id)
            if value is None:
                continue
            key = _distinct_value_key(value)
            if key in seen:
                continue
            seen.add(key)
            values.append(value)
        return values


def _expression_score_key(expression: Expression) -> str:
    """Stable cache key so worker RPC clones of the same Expression share scores."""

    return json.dumps(expression.to_record(), sort_keys=True, separators=(",", ":"))


def _is_search_terminal_expression(expression: Expression) -> bool:
    return bool(expression.operations and expression.operations[-1].kind in ("Lexical", "Semantic"))


def _distinct_value_key(value: Any) -> str:
    """Stable key for values-unit uniqueness (JSON when possible, else repr)."""

    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


def _search_ranking_expressions(ranking: Ranking) -> list[Expression]:
    found: list[Expression] = []
    _collect_search_ranking_expressions(ranking, found)
    return found


def _collect_search_ranking_expressions(ranking: Ranking, found: list[Expression]) -> None:
    if ranking.expression is not None:
        expression = ranking.expression
        if _is_search_terminal_expression(expression):
            found.append(expression)
        return
    if ranking.left is not None:
        _collect_search_ranking_expressions(ranking.left, found)
    if isinstance(ranking.right, Ranking):
        _collect_search_ranking_expressions(ranking.right, found)


def _search_predicate_expressions(predicate: Predicate) -> list[Expression]:
    found: list[Expression] = []
    _collect_search_predicate_expressions(predicate, found)
    return found


def _collect_search_predicate_expressions(predicate: Predicate, found: list[Expression]) -> None:
    if predicate.operator in ("and", "or"):
        _collect_search_predicate_expressions(predicate.left, found)
        _collect_search_predicate_expressions(predicate.right, found)
        return
    if predicate.operator == "not":
        _collect_search_predicate_expressions(predicate.left, found)
        return
    if isinstance(predicate.left, Expression) and _is_search_terminal_expression(predicate.left):
        found.append(predicate.left)
    if isinstance(predicate.right, Expression) and _is_search_terminal_expression(predicate.right):
        found.append(predicate.right)
