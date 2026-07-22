"""Where this file sits in Quail

Facade flow::

    agent code
      -> group=G0 / G1 / G0.where(...)     # WHICH rows or columns (group.py)
      -> unit=entries / fields / …        # WHAT to return from them (this file)
      -> retrieve(unit=…, group=…, …)     # namespace stub -> later engine

``Unit`` answers the retrieve/count question: "return what kind of thing?"
It is not the population itself — that is ``GroupExpr`` (``G0``, ``G1``, …).

Examples::

    retrieve(unit=entries, group=G0, limit=10)
    #          ^^^^^^^^^^^  ^^^^^^^
    #          return Entry   from all rows
    #          handles

    retrieve(unit=fields, group=G1, limit=50)
    #          ^^^^^^^^^^  ^^^^^^^
    #          return Field  from all columns
    #          definitions

    retrieve(unit=Unit("values", Field("topic")), group=G0, limit=100)
    #          distinct present topic values across selected entries

Scopes:

- ``"entries"`` — row handles (optional ``field``: present values of that
  field, still one result per selected entry that has it)
- ``"fields"`` — column definitions; must not pass ``field=``
- ``"values"`` — distinct present values of ``field``; ``field`` required

Builtins ``entries`` and ``fields`` are just ``Unit("entries")`` and
``Unit("fields")``. Ranking is allowed for entry-aligned units, not for
``fields`` or distinct ``values`` (see docs/api.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quail.analysis.errors import QuailSyntaxError
from quail.analysis.field import Field

_ALLOWED_SCOPES = frozenset({"entries", "fields", "values"})


@dataclass(frozen=True, slots=True)
class Unit:
    """What retrieve/count should return (not which population — that is group=)."""

    scope: str
    field: Field | None = None

    def __post_init__(self) -> None:
        _require_allowed_scope(self.scope)
        _require_field_type(self.field)
        _require_scope_field_combo(self.scope, self.field)

    def to_record(self) -> dict[str, Any]:
        """Plain dict for debugging / later serialization."""

        return {
            "scope": self.scope,
            "field": None if self.field is None else self.field.to_record(),
        }


# Injected builtins — same objects every time (immutable Units).
entries = Unit(scope="entries")
fields = Unit(scope="fields")


def _require_allowed_scope(scope: Any) -> None:
    if scope not in _ALLOWED_SCOPES:
        raise QuailSyntaxError('Unit scope must be "entries", "fields", or "values"')


def _require_field_type(field: Any) -> None:
    if field is not None and not isinstance(field, Field):
        raise QuailSyntaxError("Unit field must be a Field or None")


def _require_scope_field_combo(scope: str, field: Field | None) -> None:
    # fields catalog: no column selector — the group already picks which Fields.
    if scope == "fields" and field is not None:
        raise QuailSyntaxError('Unit("fields", field) is invalid')
    # distinct values: must say which column to uniquify.
    if scope == "values" and field is None:
        raise QuailSyntaxError('Unit("values", field) requires a Field')
    # entries + field is allowed: present values aligned to entries.
    # entries + field=None is the default (return Entry handles).
