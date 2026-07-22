"""Where this file sits in Quail

Facade flow::

    agent code
      -> Field("content")              # this module: name a column
      -> Expression(Field(...), …)     # read/transform that column's values
      -> Predicate / Group / Ranking
      -> later: retrieve/tag (engine) resolves the name against real data

A ``Field`` is only a *reference* to a column (like a column header), not the
cell values in any row. It does not read Turso. Example::

    Field("content")
         |
         v
    Dataset columns:  id  |  content  |  speaker
                           ^^^^^^^^^^
                           this is what Field("content") points at

Wrong (raises here)::

    Field("topic") == "climate"

Right (builds a Predicate in expression/predicate modules)::

    Expression(Field("topic"), Value()) == "climate"

``kind`` is optional metadata: ``None`` (resolve by name), ``"source"``
(imported, immutable), or ``"analysis"`` (session tags from create_field).
Whether that column actually exists is checked later by the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

from quail.analysis.errors import QuailSyntaxError

_ALLOWED_KINDS = frozenset({None, "source", "analysis"})


@dataclass(eq=False, frozen=True, slots=True)
class Field:
    """Named column/tag reference.

    Dataclass flags:
    - frozen: recipe pieces are immutable after construction
    - eq=False: we own ``==`` so Field == "x" errors instead of returning False
    - slots: fixed attributes only
    """

    name: str
    kind: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty_name(self.name)
        _require_allowed_kind(self.kind)

    def to_record(self) -> dict[str, Any]:
        """Plain dict for debugging / later serialization — not agent-facing day to day."""

        return {"name": self.name, "kind": self.kind}

    # --- equality: Field↔Field ok for hosts/tests; Field↔value is always wrong ---

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Field):
            # Agents mean "values in this column == …" — that needs Expression.
            raise QuailSyntaxError(
                "Field comparisons are not supported; compare values with "
                f"Expression(Field({self.name!r}), Value())"
            )
        return (self.name, self.kind) == (other.name, other.kind)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        # Matches equality key so Fields can live in sets/dicts if needed.
        return hash((self.name, self.kind))

    # --- ordering: columns are not ordered; numeric Expressions are ---

    def __lt__(self, other: object) -> NoReturn:
        del other
        _reject_field_ordering(self.name)

    def __le__(self, other: object) -> NoReturn:
        del other
        _reject_field_ordering(self.name)

    def __gt__(self, other: object) -> NoReturn:
        del other
        _reject_field_ordering(self.name)

    def __ge__(self, other: object) -> NoReturn:
        del other
        _reject_field_ordering(self.name)


def _require_nonempty_name(name: Any) -> None:
    if not isinstance(name, str) or not name:
        raise QuailSyntaxError("Field name must be a non-empty string")


def _require_allowed_kind(kind: Any) -> None:
    if kind not in _ALLOWED_KINDS:
        raise QuailSyntaxError("Field kind must be source, analysis, or None")


def _reject_field_ordering(field_name: str) -> NoReturn:
    raise QuailSyntaxError(
        "Field ordering is not supported; compare a numeric expression such as "
        f"Expression(Field({field_name!r}), AsNumber())"
    )
