"""Groups: symbolic populations of entries or fields."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


from quail.analysis.entry import Entry
from quail.analysis.errors import QuailScopeError, QuailSyntaxError
from quail.analysis.field import Field
from quail.analysis.predicate import Predicate


@dataclass(frozen=True, slots=True)
class GroupExpr:
    scope: str
    name: str | None = None
    predicate: Predicate | None = None
    members: tuple[Any, ...] | None = None
    left: GroupExpr | None = None
    operator: str | None = None
    right: GroupExpr | None = None

    def __init__(
        self,
        scope: str,
        predicate: Predicate | None = None,
        members: list[Any] | None = None,
        name: str | None = None,
        left: GroupExpr | None = None,
        operator: str | None = None,
        right: GroupExpr | None = None,
    ) -> None:
        if scope not in ("entries", "fields"):
            raise QuailSyntaxError('GroupExpr scope must be "entries" or "fields"')
        if predicate is not None and not isinstance(predicate, Predicate):
            raise QuailSyntaxError("GroupExpr predicate must be a Predicate")
        if operator is not None and operator not in ("and", "or", "not"):
            raise QuailSyntaxError("GroupExpr operator must be and, or, or not")

        named = name is not None
        filtered = predicate is not None
        membered = members is not None
        composed = operator is not None or left is not None or right is not None
        form_count = sum([named, filtered, membered, composed])
        if form_count == 0:
            raise QuailSyntaxError("GroupExpr requires name=, predicate=, members=, or composition")
        if form_count > 1:
            raise QuailSyntaxError("GroupExpr accepts exactly one population form")

        if name is not None and (scope, name) not in (("entries", "G0"), ("fields", "G1")):
            raise QuailSyntaxError("Built-in groups must be G0 for entries or G1 for fields")
        if scope == "fields" and predicate is not None:
            raise QuailScopeError("Predicates are entry-scoped and cannot define field groups")
        if members is not None:
            if not isinstance(members, list):
                raise QuailSyntaxError("GroupExpr members must be provided as a list")
            members = _unique_members(scope, members)
        if composed:
            if not isinstance(left, GroupExpr) or left.scope != scope:
                raise QuailScopeError("Group composition requires a compatible left GroupExpr")
            if operator == "not":
                if right is not None:
                    raise QuailSyntaxError("Group inversion accepts only a left GroupExpr")
            elif operator in ("and", "or"):
                if not isinstance(right, GroupExpr) or right.scope != scope:
                    raise QuailScopeError("Group composition requires a compatible right GroupExpr")
            else:
                raise QuailSyntaxError("GroupExpr composition requires and, or, or not")

        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "predicate", predicate)
        object.__setattr__(self, "members", None if members is None else tuple(members))
        object.__setattr__(self, "left", left)
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "right", right)

    def where(self, predicate: Predicate) -> GroupExpr:
        if self.scope != "entries":
            raise QuailScopeError("where(...) is valid only on entry-scoped groups")
        if not isinstance(predicate, Predicate):
            raise QuailSyntaxError(
                "where(...) requires a Predicate created by comparing an Expression"
            )
        return self & GroupExpr(scope="entries", predicate=predicate)

    def to_record(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "name": self.name,
            "predicate": None if self.predicate is None else self.predicate.to_record(),
            "members": (
                None
                if self.members is None
                else [_member_record(member) for member in self.members]
            ),
            "left": None if self.left is None else self.left.to_record(),
            "operator": self.operator,
            "right": None if self.right is None else self.right.to_record(),
        }

    def __and__(self, other: GroupExpr) -> GroupExpr:
        return GroupExpr(scope=self.scope, left=self, operator="and", right=other)

    def __or__(self, other: GroupExpr) -> GroupExpr:
        return GroupExpr(scope=self.scope, left=self, operator="or", right=other)

    def __invert__(self) -> GroupExpr:
        return GroupExpr(scope=self.scope, left=self, operator="not")

    def __bool__(self) -> bool:
        raise QuailSyntaxError("Compose groups with &, |, and ~; do not use them in if/while")

    def __iter__(self) -> Iterator[Any]:
        raise QuailSyntaxError(
            "GroupExpr values are symbolic; use retrieve(group=group, limit=...) instead"
        )

    def __getitem__(self, index: Any) -> Any:
        del index
        raise QuailSyntaxError(
            "GroupExpr values are symbolic; retrieve(group=group, limit=...)[index] instead"
        )


def _member_record(member: Any) -> dict[str, Any]:
    return member.to_record()


def _unique_members(scope: str, members: list[Any]) -> list[Any]:
    unique: list[Any] = []
    seen: set[Any] = set()
    for member in members:
        if scope == "entries":
            if not isinstance(member, Entry):
                raise QuailSyntaxError("Entry-scoped group members must be Entry handles")
            key: Any = member.id
        else:
            if not isinstance(member, Field):
                raise QuailSyntaxError("Field-scoped group members must be Field references")
            key = (member.name, member.kind)
        if key in seen:
            continue
        seen.add(key)
        unique.append(member)
    return unique


G0 = GroupExpr(scope="entries", name="G0")
G1 = GroupExpr(scope="fields", name="G1")
