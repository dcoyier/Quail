"""Host QueryEngine: evaluate plans against catalog + in-memory overlay."""

from __future__ import annotations

import math
import re
from typing import Any

from quail.analysis.entry import Entry, make_entry
from quail.analysis.errors import QuailFieldError, QuailRuntimeError, QuailSyntaxError
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
from quail.analysis.unit import Unit
from quail.datasets.catalog import source_entries, source_values
from quail.datasets.db import CoreDb
from quail.search import SimilarityService
from quail.session.models import FieldCreate, Scope, ValueDelete, ValueWrite
from quail.session.overlay import analysis_fields, analysis_values, catalog_fields


class QueryEngine:
    """Evaluate retrieve/count/mutations for one scope; stage overlay in memory."""

    def __init__(
        self,
        db: CoreDb,
        scope: Scope,
        *,
        similarity: SimilarityService | None = None,
    ) -> None:
        self._db = db
        self._scope = scope
        self._similarity = similarity
        self._mutations: list[FieldCreate | ValueWrite | ValueDelete] = []
        self._created_fields: dict[str, Field] = {}
        # (field_name, entry_id) -> value | _ABSENT sentinel for deletes in overlay
        self._value_overlay: dict[tuple[str, str], Any] = {}
        self._absent = object()

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

        entry_ids = self._evaluate_entry_group(plan.group)
        entry_ids = self._apply_ranking(entry_ids, plan.ranking)
        entry_ids = self._apply_limit(entry_ids, plan.limit, plan.order)

        if isinstance(plan.unit, Expression):
            return [self._eval_expression(plan.unit, entry_id) for entry_id in entry_ids]

        assert isinstance(plan.unit, Unit)
        if plan.unit.scope == "entries":
            if plan.unit.field is None:
                return [self._make_entry(entry_id) for entry_id in entry_ids]
            return [
                self._read_field_value(plan.unit.field.name, entry_id) for entry_id in entry_ids
            ]
        if plan.unit.scope == "values":
            assert plan.unit.field is not None
            seen: set[str] = set()
            values: list[Any] = []
            for entry_id in entry_ids:
                value = self._read_field_value(plan.unit.field.name, entry_id)
                if value is None:
                    continue
                key = repr(value)
                if key in seen:
                    continue
                seen.add(key)
                values.append(value)
            return values
        raise QuailSyntaxError(f"Unsupported unit scope: {plan.unit.scope}")

    def count(self, plan: CountPlan) -> int:
        if isinstance(plan.unit, Unit) and plan.unit.scope == "fields":
            return len(self._evaluate_field_group(plan.group))
        entry_ids = self._evaluate_entry_group(plan.group)
        if isinstance(plan.unit, Expression):
            return len(entry_ids)
        assert isinstance(plan.unit, Unit)
        if plan.unit.scope == "entries":
            if plan.unit.field is None:
                return len(entry_ids)
            return sum(
                1
                for entry_id in entry_ids
                if self._read_field_value(plan.unit.field.name, entry_id) is not None
            )
        if plan.unit.scope == "values":
            assert plan.unit.field is not None
            seen: set[str] = set()
            for entry_id in entry_ids:
                value = self._read_field_value(plan.unit.field.name, entry_id)
                if value is None:
                    continue
                seen.add(repr(value))
            return len(seen)
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
        return self._created_fields[name]

    def tag(self, plan: TagPlan) -> None:
        field_name = plan.field.name
        self._require_analysis_field(field_name)
        for entry_id in self._tag_entry_ids(plan.group):
            self._value_overlay[(field_name, entry_id)] = plan.value
            self._mutations.append(ValueWrite(field_name, entry_id, plan.value))

    def untag(self, plan: UntagPlan) -> None:
        field_name = plan.field.name
        self._require_analysis_field(field_name)
        for entry_id in self._tag_entry_ids(plan.group):
            current = self._read_field_value(field_name, entry_id)
            if current is None:
                continue
            if plan.value is not None and current != plan.value:
                continue
            self._value_overlay[(field_name, entry_id)] = self._absent
            self._mutations.append(ValueDelete(field_name, entry_id, plan.value))

    def entry_value(
        self,
        entry: Entry,
        field: Field | str,
        default: Any = None,
    ) -> Any:
        field_name = field.name if isinstance(field, Field) else field
        if not isinstance(field_name, str) or not field_name:
            raise QuailSyntaxError("field must be a Field or non-empty string")
        value = self._read_field_value(field_name, entry.id)
        return default if value is None else value

    def entry_fields(self, entry: Entry) -> list[Field]:
        present: list[Field] = []
        for field in self._catalog():
            if self._read_field_value(field.name, entry.id) is not None:
                present.append(Field(field.name, kind=field.kind))
        return present

    def _catalog(self) -> list[Any]:
        from quail.session.models import CatalogField

        result: list[CatalogField] = list(catalog_fields(self._db, self._scope))
        seen = {field.name for field in result}
        for name in self._created_fields:
            if name not in seen:
                result.append(CatalogField(name=name, kind="analysis", position=len(result)))
        return result

    def _make_entry(self, entry_id: str) -> Entry:
        return make_entry(
            entry_id,
            dataset_id=self._scope.dataset_id,
            dataset_version_id=self._scope.dataset_version_id,
            dataset=self._scope.dataset_id,
        )

    def _tag_entry_ids(self, group: GroupExpr | tuple[Entry, ...]) -> list[str]:
        if isinstance(group, tuple):
            return [entry.id for entry in group]
        return self._evaluate_entry_group(group)

    def _require_analysis_field(self, name: str) -> None:
        for field in self._catalog():
            if field.name == name:
                if field.kind != "analysis":
                    raise QuailFieldError(f"Cannot tag source field: {name}")
                return
        raise QuailFieldError(f"Unknown analysis field: {name}")

    def _all_entry_ids(self) -> list[str]:
        return [
            entry.id
            for entry in source_entries(
                self._db,
                self._scope.workspace_id,
                self._scope.dataset_id,
                self._scope.dataset_version_id,
            )
        ]

    def _evaluate_entry_group(self, group: GroupExpr) -> list[str]:
        if group.scope != "entries":
            raise QuailSyntaxError("Expected an entry-scoped group")
        if group.name == "G0":
            return self._all_entry_ids()
        if group.members is not None:
            return [member.id for member in group.members]
        if group.predicate is not None:
            return [
                entry_id
                for entry_id in self._all_entry_ids()
                if self._eval_predicate(group.predicate, entry_id)
            ]
        if group.operator == "and":
            assert group.left is not None and group.right is not None
            right_ids = set(self._evaluate_entry_group(group.right))
            return [
                entry_id
                for entry_id in self._evaluate_entry_group(group.left)
                if entry_id in right_ids
            ]
        if group.operator == "or":
            assert group.left is not None and group.right is not None
            seen: set[str] = set()
            result: list[str] = []
            for entry_id in self._evaluate_entry_group(group.left) + self._evaluate_entry_group(
                group.right
            ):
                if entry_id not in seen:
                    seen.add(entry_id)
                    result.append(entry_id)
            return result
        if group.operator == "not":
            assert group.left is not None
            excluded = set(self._evaluate_entry_group(group.left))
            return [entry_id for entry_id in self._all_entry_ids() if entry_id not in excluded]
        raise QuailSyntaxError("Unsupported entry group form")

    def _evaluate_field_group(self, group: GroupExpr) -> list[Field]:
        if group.scope != "fields":
            raise QuailSyntaxError("Expected a field-scoped group")
        catalog = self._catalog()
        if group.name == "G1":
            return [Field(field.name, kind=field.kind) for field in catalog]
        if group.members is not None:
            return list(group.members)
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

    def _eval_predicate(self, predicate: Predicate, entry_id: str) -> bool:
        if predicate.operator == "and":
            return self._eval_predicate(predicate.left, entry_id) and self._eval_predicate(
                predicate.right, entry_id
            )
        if predicate.operator == "or":
            return self._eval_predicate(predicate.left, entry_id) or self._eval_predicate(
                predicate.right, entry_id
            )
        if predicate.operator == "not":
            return not self._eval_predicate(predicate.left, entry_id)

        left = self._eval_expression(predicate.left, entry_id)
        right = predicate.right
        if isinstance(right, Expression):
            right = self._eval_expression(right, entry_id)
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

    def _eval_expression(self, expression: Expression, entry_id: str) -> Any:
        value: Any = self._read_field_value(expression.root.name, entry_id)
        for operation in expression.operations:
            value = self._apply_operation(operation, value)
        return value

    def _apply_operation(self, operation: Operation, value: Any) -> Any:
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
            raise QuailRuntimeError(
                "Lexical is not wired yet",
                repair_hint="Lexical FTS lands in a later slice; use Semantic or filters for now.",
            )
        if kind == "Semantic":
            if self._similarity is None:
                raise QuailRuntimeError(
                    "Semantic search is not configured",
                    repair_hint=(
                        "Set core.search_database, [providers.*], and [datasets.embedding], "
                        "re-run quail, then retry the whole exec."
                    ),
                )
            return self._similarity.semantic_score(
                workspace_id=self._scope.workspace_id,
                dataset_id=self._scope.dataset_id,
                version_id=self._scope.dataset_version_id,
                corpus=value,
                query_record=operation.params["query"],
                input_aggregation=operation.params.get("input_aggregation"),
                target_aggregation=operation.params.get("target_aggregation"),
            )
        raise QuailSyntaxError(f"Unsupported operation: {kind}")

    def _apply_regex(self, operation: Operation, value: Any) -> Any:
        pattern = str(operation.params["pattern"])
        flags = int(operation.params.get("flags", 0))
        compiled = re.compile(pattern, flags)
        if operation.kind == "RegexSearch":
            if value is None:
                return None
            text = value if isinstance(value, str) else str(value)
            match = compiled.search(text)
            return None if match is None else match.group(0)
        if operation.kind == "RegexFindAll":
            if value is None:
                return []
            text = value if isinstance(value, str) else str(value)
            return compiled.findall(text)
        # RegexSub
        replacement = str(operation.params["replacement"])
        if value is None:
            return None
        if isinstance(value, str):
            return compiled.sub(replacement, value)
        if isinstance(value, list):
            return [
                compiled.sub(replacement, item) if isinstance(item, str) else item for item in value
            ]
        raise QuailRuntimeError("RegexSub requires text or list[text]")

    def _read_field_value(self, field_name: str, entry_id: str) -> Any:
        key = (field_name, entry_id)
        if key in self._value_overlay:
            value = self._value_overlay[key]
            return None if value is self._absent else value

        kind = self._field_kind(field_name)
        if kind == "source":
            values = source_values(
                self._db,
                self._scope.workspace_id,
                self._scope.dataset_id,
                self._scope.dataset_version_id,
                field_name,
                entry_ids=[entry_id],
            )
            return values[0] if values else None
        if kind == "analysis":
            if field_name in self._created_fields and not any(
                field.name == field_name for field in analysis_fields(self._db, self._scope)
            ):
                return None
            values = analysis_values(
                self._db,
                self._scope,
                field_name,
                entry_ids=[entry_id],
            )
            return values[0] if values else None
        raise QuailFieldError(f"Unknown field: {field_name}")

    def _field_kind(self, field_name: str) -> str | None:
        for field in self._catalog():
            if field.name == field_name:
                return field.kind
        return None

    def _apply_ranking(self, entry_ids: list[str], ranking: Ranking) -> list[str]:
        if (
            ranking.expression is None
            and ranking.left is None
            and ranking.operator is None
            and ranking.right is None
        ):
            return entry_ids
        semantic_scores = self._precompute_semantic_ranking_scores(ranking, entry_ids)
        scored = [
            (self._score_ranking(ranking, entry_id, semantic_scores), entry_id)
            for entry_id in entry_ids
        ]
        scored.sort(key=lambda item: (-item[0], entry_ids.index(item[1])))
        return [entry_id for _, entry_id in scored]

    def _precompute_semantic_ranking_scores(
        self,
        ranking: Ranking,
        entry_ids: list[str],
    ) -> dict[int, dict[str, float | None]]:
        """Batch-score every Semantic-terminal ranking leaf once."""

        if self._similarity is None or not entry_ids:
            return {}
        expressions = _semantic_ranking_expressions(ranking)
        if not expressions:
            return {}
        cached: dict[int, dict[str, float | None]] = {}
        for expression in expressions:
            key = id(expression)
            if key in cached:
                continue
            operation = expression.operations[-1]
            prefix_ops = expression.operations[:-1]
            corpus_by_entry: dict[str, Any] = {}
            for entry_id in entry_ids:
                if prefix_ops:
                    corpus_by_entry[entry_id] = self._eval_expression(
                        Expression(expression.root, *prefix_ops), entry_id
                    )
                else:
                    corpus_by_entry[entry_id] = self._read_field_value(
                        expression.root.name, entry_id
                    )
            cached[key] = self._similarity.semantic_scores_for_entries(
                workspace_id=self._scope.workspace_id,
                dataset_id=self._scope.dataset_id,
                version_id=self._scope.dataset_version_id,
                corpus_by_entry=corpus_by_entry,
                query_record=dict(operation.params["query"]),
                input_aggregation=operation.params.get("input_aggregation"),
                target_aggregation=operation.params.get("target_aggregation"),
            )
        return cached

    def _score_ranking(
        self,
        ranking: Ranking,
        entry_id: str,
        semantic_scores: dict[int, dict[str, float | None]] | None = None,
    ) -> float:
        if ranking.expression is not None:
            expression = ranking.expression
            if (
                semantic_scores is not None
                and expression.operations
                and expression.operations[-1].kind == "Semantic"
                and id(expression) in semantic_scores
            ):
                value = semantic_scores[id(expression)].get(entry_id)
            else:
                value = self._eval_expression(expression, entry_id)
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
            return self._score_ranking(
                ranking.left, entry_id, semantic_scores
            ) + self._score_ranking(ranking.right, entry_id, semantic_scores)
        if ranking.operator == "*" and ranking.left is not None:
            weight = float(ranking.right)
            return self._score_ranking(ranking.left, entry_id, semantic_scores) * weight
        raise QuailSyntaxError("Unsupported ranking form")

    def _apply_limit(self, items: list[Any], limit: int, order: str) -> list[Any]:
        if not items:
            return []
        if order == "top":
            return items[:limit]
        if order == "bottom":
            return items[-limit:] if limit < len(items) else list(items)
        # middle
        if len(items) <= limit:
            return list(items)
        start = max(0, (len(items) - limit) // 2)
        return items[start : start + limit]


def _semantic_ranking_expressions(ranking: Ranking) -> list[Expression]:
    found: list[Expression] = []
    _collect_semantic_ranking_expressions(ranking, found)
    return found


def _collect_semantic_ranking_expressions(ranking: Ranking, found: list[Expression]) -> None:
    if ranking.expression is not None:
        expression = ranking.expression
        if expression.operations and expression.operations[-1].kind == "Semantic":
            found.append(expression)
        return
    if ranking.left is not None:
        _collect_semantic_ranking_expressions(ranking.left, found)
    if isinstance(ranking.right, Ranking):
        _collect_semantic_ranking_expressions(ranking.right, found)
