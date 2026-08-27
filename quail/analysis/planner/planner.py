"""Validate facade arguments into frozen evaluation plans."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from quail.analysis.entry import Entry
from quail.analysis.errors import QuailScopeError, QuailSyntaxError
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import GroupExpr
from quail.analysis.ranking import Ranking
from quail.analysis.unit import Unit, entries


def require_tag_value(value: Any, *, label: str = "Tag value") -> Any:
    """Reject nested None and non-JSON-like tag payloads."""

    return _require_tag_value(value, label=label, stack=set())


def _require_tag_value(value: Any, *, label: str, stack: set[int]) -> Any:
    if value is None:
        raise QuailSyntaxError(f"{label} cannot contain None")
    if isinstance(value, bool | str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QuailSyntaxError(f"{label} cannot contain non-finite floats")
        return value
    if isinstance(value, list):
        identity = id(value)
        if identity in stack:
            raise QuailSyntaxError(f"{label} cannot contain cycles")
        stack.add(identity)
        try:
            for item in value:
                _require_tag_value(item, label=label, stack=stack)
        finally:
            stack.remove(identity)
        return value
    if isinstance(value, dict):
        identity = id(value)
        if identity in stack:
            raise QuailSyntaxError(f"{label} cannot contain cycles")
        stack.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise QuailSyntaxError(f"{label} object keys must be strings")
                _require_tag_value(item, label=label, stack=stack)
        finally:
            stack.remove(identity)
        return value
    raise QuailSyntaxError(f"{label} does not support {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class RetrievePlan:
    unit: Unit | Expression
    group: GroupExpr
    limit: int
    order: str
    ranking: Ranking


@dataclass(frozen=True, slots=True)
class CountPlan:
    unit: Unit | Expression
    group: GroupExpr


@dataclass(frozen=True, slots=True)
class CreateFieldPlan:
    field: Field


@dataclass(frozen=True, slots=True)
class TagPlan:
    group: GroupExpr | tuple[Entry, ...]
    field: Field
    value: Any


@dataclass(frozen=True, slots=True)
class UntagPlan:
    group: GroupExpr | tuple[Entry, ...]
    field: Field
    value: Any | None


def plan_retrieve(
    unit: Any = entries,
    group: Any = None,
    limit: int = 1,
    order: str = "top",
    rank: Ranking | None = None,
) -> RetrievePlan:
    from quail.analysis.group import G0

    if group is None:
        group = G0
    unit = _require_unit_or_expression(unit)
    group = _require_group(group)
    limit = _require_positive_int(limit, label="limit")
    order = _require_order(order)
    ranking = Ranking() if rank is None else rank
    if not isinstance(ranking, Ranking):
        raise QuailSyntaxError("rank must be a Ranking")
    _require_unit_group_scope(unit, group)
    if (
        isinstance(unit, Unit)
        and unit.scope in ("fields", "values")
        and not _ranking_empty(ranking)
    ):
        raise QuailScopeError("fields and values units cannot be ranked")
    return RetrievePlan(unit=unit, group=group, limit=limit, order=order, ranking=ranking)


def plan_count(unit: Any = entries, group: Any = None) -> CountPlan:
    from quail.analysis.group import G0

    if group is None:
        group = G0
    unit = _require_unit_or_expression(unit)
    group = _require_group(group)
    _require_unit_group_scope(unit, group)
    return CountPlan(unit=unit, group=group)


def plan_create_field(field: str | Field) -> CreateFieldPlan:
    if isinstance(field, str):
        if not field.strip():
            raise QuailSyntaxError("create_field name must be a non-empty string")
        resolved = Field(field.strip(), kind="analysis")
    elif isinstance(field, Field):
        if field.kind not in (None, "analysis"):
            raise QuailSyntaxError('create_field Field kind must be "analysis" or None')
        name = field.name.strip()
        if not name:
            raise QuailSyntaxError("create_field name must be a non-empty string")
        resolved = Field(name, kind="analysis")
    else:
        raise QuailSyntaxError("create_field requires a string name or Field")
    return CreateFieldPlan(field=resolved)


def plan_tag(
    group: GroupExpr | list[Any],
    field: Field,
    value: Any,
) -> TagPlan:
    if not isinstance(field, Field):
        raise QuailSyntaxError("tag field must be a Field")
    if value is None:
        raise QuailSyntaxError("tag value cannot be None")
    require_tag_value(value)
    return TagPlan(group=_require_tag_group(group), field=field, value=value)


def plan_untag(
    group: GroupExpr | list[Any],
    field: Field,
    value: Any | None = None,
) -> UntagPlan:
    if not isinstance(field, Field):
        raise QuailSyntaxError("untag field must be a Field")
    if value is not None:
        require_tag_value(value, label="Untag value")
    return UntagPlan(group=_require_tag_group(group), field=field, value=value)


def _require_unit_or_expression(unit: Any) -> Unit | Expression:
    if isinstance(unit, GroupExpr):
        raise QuailSyntaxError("unit cannot be a GroupExpr")
    if isinstance(unit, str):
        raise QuailSyntaxError("unit must be a Unit or Expression, not a string")
    if isinstance(unit, Unit | Expression):
        return unit
    raise QuailSyntaxError("unit must be a Unit or Expression")


def _require_group(group: Any) -> GroupExpr:
    if not isinstance(group, GroupExpr):
        raise QuailSyntaxError("group must be a GroupExpr")
    return group


def _require_tag_group(group: GroupExpr | list[Any]) -> GroupExpr | tuple[Entry, ...]:
    if isinstance(group, GroupExpr):
        if group.scope != "entries":
            raise QuailScopeError("tag/untag require an entry-scoped group")
        return group
    if isinstance(group, list):
        if not group:
            return tuple()
        entries_list: list[Entry] = []
        for item in group:
            if not isinstance(item, Entry):
                raise QuailSyntaxError("tag/untag list members must be Entry handles")
            entries_list.append(item)
        return tuple(entries_list)
    raise QuailSyntaxError("tag/untag group must be a GroupExpr or list of Entry")


def _require_positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise QuailSyntaxError(f"{label} must be a positive int")
    return value


def _require_order(order: Any) -> str:
    if order not in ("top", "middle", "bottom"):
        raise QuailSyntaxError('order must be "top", "middle", or "bottom"')
    return order


def _require_unit_group_scope(unit: Unit | Expression, group: GroupExpr) -> None:
    if isinstance(unit, Expression):
        if group.scope != "entries":
            raise QuailScopeError("Expression units require an entry-scoped group")
        return
    if unit.scope == "fields":
        if group.scope != "fields":
            raise QuailScopeError('Unit("fields") requires a field-scoped group')
    elif group.scope != "entries":
        raise QuailScopeError(f'Unit("{unit.scope}") requires an entry-scoped group')


def _ranking_empty(ranking: Ranking) -> bool:
    return (
        ranking.expression is None
        and ranking.left is None
        and ranking.operator is None
        and ranking.right is None
    )
