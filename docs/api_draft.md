# Quail Analysis API

> **Unpublished.** This is a draft of the model-facing analysis contract.
> `quail_get_api_docs` still serves [`api.md`](api.md).

Quail lets you analyze a private dataset by writing bounded Python. You build
**symbolic** recipes (filters, scores, groups), **materialize** bounded lists
with `retrieve`, measure populations with `count`, write session-only analysis
values with `tag`, and **print** to return text in `printed_output`. Imported
source data never changes.

---

## How you call Quail

```text
quail_exec(session_id, dataset_id, code, time_window="standard")
```

Pass arguments by name. This is a **host MCP tool**, not a name inside the
sandbox.

| Argument | Meaning |
| --- | --- |
| `session_id` | Durable analysis context in one workspace. Bindings persist across successful calls in this session. Analysis fields and tagged values persist for this session and dataset version. A session belongs to the workspace in which it was created; reuse it serially. After `quail_switch_workspace`, start a new session. Unrestricted deployments have one fixed workspace. Run one `quail_exec` at a time per `session_id`. Overlap with another `quail_exec` or `quail_export_csv` on the same session fails with stable code `session_busy`. Process-wide execution capacity exhausted fails with `server_busy`. Both report `error_class` `"QuailRuntimeError"`. Retry the same session; do not start a new one. |
| `dataset_id` | Selects one dataset. The call uses that dataset's active immutable version; `entry.dataset_version_id` reports which version you got. |
| `code` | Bounded Quail Python (no imports, no files, no network). |
| `time_window` | `"standard"` (30 seconds wall-clock, 15 seconds CPU) or `"extended"` (100 seconds wall-clock, 60 seconds CPU). Omitted or `null` means `"standard"`. Worker resident memory is capped at 256 MiB for both. |

**Success:** `{"printed_output": "<the exact contents of the print buffer>"}`.

**Failure:** a tool error:

```text
{"execution_id": null, "diagnostic": {
    "error_class": str,
    "stable_error_code": str,
    "message": str,
    "repair_hint": str   # omitted when absent
}}
```

`execution_id` is `null` on every current `quail_exec` failure. MCP
argument-validation failures may use the client's native tool-error form
instead of this envelope.

Nothing partial is kept if `quail_exec` fails — no overlay writes (`create_field`,
`tag`, `untag`), no bindings, no printed text.

Only `print(...)` adds text to `printed_output`. The value of a bare Python
expression statement is discarded.

---

## Vocabulary

| Term | Meaning |
| --- | --- |
| **Entry** | One row in the dataset. |
| **Field** | One source or analysis column. The CSV `id` column is `entry.id`, not `Field("id")`. |
| **Expression** | A recipe that reads or transforms one field's value for each entry. |
| **Predicate** | A true-or-false recipe for each entry. |
| **GroupExpr** | A symbolic population of entries or fields. It is not iterable. Pass it to `retrieve` for a list, or to `count` for a size. |
| **Unit** | What `retrieve` lists and what `count` sizes. `count` always returns `int`. An Expression unit is computed values for `retrieve` and group size for `count` (the expression is not run). |
| **Ranking** | How to score and order entry-scoped candidates. |
| **Binding** | A top-level name that survives a successful exec in this session. |
| **Overlay write** | `create_field`, `tag`, or `untag`. These change only the session overlay. |

**Symbolic vs materialized:** constructing `Expression(...)` or
`G0.where(...)` does not read dataset cells. Symbolic recipes evaluate at
`retrieve`, `count`, `tag`, and `untag`. `entry.value(...)` and
`entry.fields()` read through the same engine.

---

## First exec

Field names differ per dataset. Inspect before assuming any:

```python
print(count(unit=fields, group=G1))
for field in retrieve(unit=fields, group=G1, limit=50):
    print(field.name, field.kind)

samples = retrieve(limit=1)
if len(samples) > 0:
    for field in samples[0].fields():
        print(field.name, repr(samples[0].value(field)))
```

Imported blank cells are absent and read as `None`. A stored `""` remains an
empty string. Imported CSV cells are stripped strings, not numbers — use
`AsNumber()` for numeric compare.

---

## What you can use (injected namespace)

No imports. These names are injected.

| Kind | Names |
| --- | --- |
| Callables | `retrieve`, `count`, `create_field`, `tag`, `untag`, `print` |
| Groups / units | `G0`, `G1`, `entries`, `fields` |
| Types | `Field`, `Unit`, `Expression`, `Predicate`, `GroupExpr`, `Ranking`, `Entry` |
| Ops | `Value`, `AsText`, `AsNumber`, `RegexSearch`, `RegexFindAll`, `RegexSub`, `Slice`, `Length`, `Lexical`, `Semantic` |
| Regex helper | `re` (flags + `re.escape` only — not Python's `re` module) |
| Errors | `QuailError`, `QuailSyntaxError`, `QuailScopeError`, `QuailFieldError`, `QuailRuntimeError` |
| Safe builtins | `abs`, `all`, `any`, `bool`, `dict`, `enumerate`, `float`, `int`, `len`, `list`, `max`, `min`, `range`, `repr`, `round`, `set`, `str`, `sum`, `tuple` |

Callables, groups, units, types, ops, `re`, and error classes are **reserved**
(cannot be assigned or deleted). Safe builtins are injected but not reserved.

- **`G0`**: all entries (import order). **`G1`**: all fields (source, then
  analysis). Retrieve fields with `retrieve(unit=fields, group=G1, ...)`.
- **`entries` / `fields`**: prebuilt `Unit` values, not groups. `entries` is
  the default unit. `fields` requires a field group such as `G1`.
  `retrieve(group=G1)` fails because the default unit is `entries`.

Compose `Predicate` values with `&`, `|`, and `~`. Compose same-scope
`GroupExpr` values with `&`, `|`, and `~`. Compare `Expression` values with
`==`, `!=`, `<`, `<=`, `>`, `>=`. Python `and` / `or` / `not` work only on
materialized values. Do not truth-test an `Expression`, `Predicate`, or
`GroupExpr` with `if`, `while`, or `bool(...)`. Do not chain comparisons
(`a < expr < b`). `is` is rejected at parse — write `== None` / `!= None`.

`Operation` is the opaque value returned by operation factories such as
`Length()`. It is not an injected constructor.

---

## Core rules

1. **One dataset per exec** — the active immutable version of `dataset_id`.
2. **Source data is frozen** — only session analysis fields and their tagged
   values can change.
3. **Print-only output** — success returns the print buffer; failure returns
   none of it.
4. **Atomic exec** — overlay writes, bindings, and printed output succeed
   together or are all discarded.
5. **Later lines see earlier tags** in the same successful run; failed runs
   roll back.
6. **No outside world** — no imports, files, network, database handles, or
   environment variables.

The selected `time_window` fixes the wall-clock and CPU limits. Resident
memory is capped at 256 MiB. Exceeding any resource limit fails the entire
exec atomically.

---

## Types

`Field`, `Unit`, `Expression`, `Predicate`, `GroupExpr`, and `Ranking` are
**symbolic values**. Constructing them does not read dataset cells. `Entry` is
a runtime handle issued by `retrieve`, not a value you construct.

Symbolic recipes evaluate at `retrieve`, `count`, `tag`, and `untag`.
`entry.value(...)` and `entry.fields()` read cells through the same engine.
After a successful run, changed bindings undergo catalog Field-kind validation
before commit. That is transaction validation, not data evaluation.

Each symbol below has a **signature**, then parameters / returns / errors /
notes. Call constructors and functions by these signatures.

Names in **Shared shapes** (`FieldKind`, `Query`, `JSONLike`, `Any`, …) are
documentation aliases. They are not injected and are not valid inside
`quail_exec`.

### Shared shapes

```text
FieldKind = "source" | "analysis" | None
GroupScope = "entries" | "fields"
UnitScope = "entries" | "fields" | "values"
Order = "top" | "middle" | "bottom"
Aggregation = "total" | "avg" | None          # None means "total"

# Resolves to one or more non-empty target texts.
# GroupExpr must be entry-scoped.
Query = str | list[str] | GroupExpr | list[Entry]

# Acyclic. Dictionary keys are strings. float means a finite float (not inf/nan).
JSONLike = None | bool | int | float | str | list[JSONLike] | dict[str, JSONLike]

# Like JSONLike, but None is forbidden at every depth.
TagValue = bool | int | float | str | list[TagValue] | dict[str, TagValue]
```

---

### `Field`

```python
class Field:
    name: str
    kind: FieldKind

    def __init__(self, name: str, kind: FieldKind = None) -> None: ...
```

A `Field` identifies a column by name. It is not a cell value.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `str` | required | Non-empty column name. Surrounding whitespace is significant (`Field(" topic ")` is not `Field("topic")`). The CSV `id` column is `entry.id`, not `Field("id")`. |
| `kind` | `"source"` \| `"analysis"` \| `None` | `None` | `None` resolves by name when the field is accessed (`retrieve`, `count`, `tag`, `untag`, `entry.value`, or an expression that reads the field). An explicit kind must match the catalog at that access. Commit of a **changed** binding fails only if that name **already exists** in this dataset catalog with a different kind; an unknown name with an explicit kind may still be bound. |

**Attributes**

| Name | Type | Description |
| --- | --- | --- |
| `.name` | `str` | Column name. |
| `.kind` | `FieldKind` | Declared kind, or `None` to resolve later. |

**Errors**

| When | Error |
| --- | --- |
| `name` is not a non-empty `str` | `QuailSyntaxError` |
| `kind` is not `"source"`, `"analysis"`, or `None` | `QuailSyntaxError` |
| Unknown name at access | `QuailFieldError` |
| Explicit `kind` does not match the catalog at access, or at commit when the name already exists with a different kind | `QuailFieldError` (the error names the registered kind — use it, or omit `kind`) |
| Compared to a non-`Field`, or ordered with `<` `<=` `>` `>=` | `QuailSyntaxError` |

**Notes**

- Two `Field` values compare equal by `(name, kind)`. `==` / `!=` against a
  non-`Field` raises `QuailSyntaxError`.
- Do not compare a Field to a cell value. Read the column with
  `Expression(field, Value())` (or a numeric / search op) and compare that.
- Commit checks only names you bind or rebind in this exec, so a restored
  binding with a stale `kind` does not fail the exec. Using that Field still
  raises `QuailFieldError`; `del saved_field` recovers.

---

### `Unit`

```python
class Unit:
    scope: UnitScope
    field: Field | None

    def __init__(self, scope: UnitScope, field: Field | None = None) -> None: ...

entries: Unit  # Unit("entries")
fields: Unit   # Unit("fields")
```

What `retrieve` lists and what `count` sizes. The population is `group=`,
not the Unit. `count` always returns `int`.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `scope` | `"entries"` \| `"fields"` \| `"values"` | required | `"entries"` selects entry handles, or present per-entry field values when `field` is set. `"fields"` selects fields. `"values"` selects distinct present field values. |
| `field` | `Field` \| `None` | `None` | Required for `"values"`. Optional for `"entries"` (present values of that field; absent cells dropped; not distinct). Forbidden for `"fields"`. |

**Legal forms**

| Call | Meaning |
| --- | --- |
| `Unit("entries")` / `entries` | Entry handles. |
| `Unit("entries", field)` | Present values of `field`, one per matching entry (absent cells dropped; duplicates kept). Items are cell values, not `Entry` handles. |
| `Unit("fields")` / `fields` | Field handles. Use with a field group such as `G1`. |
| `Unit("values", field)` | Distinct present values over the **full** group, first-seen in the group's own order. Distinctness is JSON-text identity (`1` and `1.0` are distinct). `limit` / `order` apply to that distinct sequence, not to entries first. |

**Errors**

| When | Error |
| --- | --- |
| `scope` is not `"entries"`, `"fields"`, or `"values"` | `QuailSyntaxError` |
| `field` is not a `Field` or `None` | `QuailSyntaxError` |
| `Unit("fields", field)` | `QuailSyntaxError` |
| `Unit("values")` with no `field` | `QuailSyntaxError` |

Unknown names and kind mismatches on a unit `Field` raise `QuailFieldError`
when the unit is used in `retrieve` or `count` (and the group is non-empty).

---

### `Entry`

```python
class Entry:
    id: str
    dataset_id: str
    dataset_version_id: str
    dataset: str

    def value(self, field: Field | str, default: Any = None) -> Any: ...
    def fields(self) -> list[Field]: ...
```

Opaque handle for one dataset row. Issued by `retrieve`. There is **no public
constructor** — calling `Entry(...)` raises `QuailSyntaxError`.

**Attributes**

| Name | Type | Description |
| --- | --- | --- |
| `.id` | `str` | Row id (the CSV `id` column). |
| `.dataset_id` | `str` | Dataset this handle belongs to. |
| `.dataset_version_id` | `str` | Immutable dataset version this handle belongs to. |
| `.dataset` | `str` | Same as `.dataset_id` on retrieve-issued handles. |

Print `entry.id` (and other scalar attributes). The `str(...)` form of an
`Entry` is a dataclass repr, not a stable serialization.

#### `Entry.value`

```python
entry.value(field: Field | str, default: Any = None) -> Any
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `field` | `Field` \| `str` | required | Column to read. A string must be non-empty. |
| `default` | `Any` | `None` | Returned when the field is absent or its stored value is `None` (CSV blanks are omitted at import, so absence is `None`, not `""`). |

**Returns:** the stored cell value, or `default` when that value is `None`.
Unknown names and kind mismatches raise; they do not use `default`.

**Errors:** `QuailSyntaxError` if `field` is not a `Field` or non-empty `str`;
`QuailFieldError` if the name is unknown or the explicit kind does not match
the catalog; `QuailScopeError` if the handle belongs to another dataset or
dataset version; `QuailRuntimeError` if called outside an active `quail_exec`.

#### `Entry.fields`

```python
entry.fields() -> list[Field]
```

**Returns:** `Field` handles present on this row (catalog fields whose cell is
not `None`), in catalog order: source fields first, then analysis fields.

**Errors:** `QuailScopeError` if the handle belongs to another dataset or
dataset version; `QuailRuntimeError` if called outside an active `quail_exec`.

---

### `Expression`

```python
class Expression:
    def __init__(self, input: Field | Expression, *operations: Operation) -> None: ...
```

Recipe that reads one field's value per entry and pipes it through typed
operations. Construction type-checks the pipeline; it does not read data.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `input` | `Field` \| `Expression` | required | Root field, or an existing expression to extend. |
| `*operations` | `Operation` | see notes | Pipeline steps from the op factories (`Value()`, `Length()`, …). |

The **resulting** pipeline must have at least one operation. When `input` is a
`Field`, that means `*operations` is non-empty. When `input` is an
`Expression`, extra ops may be omitted (clone) or appended. `Value()` is the
identity when reading a field as-is — it is **not** required; `Length()`,
`AsText()`, regex, and search ops may be first. If `Value()` appears, it must
be first. If `Lexical(...)` / `Semantic(...)` appear, they must be last.

**Returns:** a new `Expression`.

**Errors**

| When | Error |
| --- | --- |
| `input` is not a `Field` or `Expression` | `QuailSyntaxError` |
| Resulting pipeline is empty | `QuailSyntaxError` |
| An argument is not an `Operation` | `QuailSyntaxError` |
| An op does not accept the previous op's kind | `QuailSyntaxError` (names both sides) |
| `Value()` appears after the first op | `QuailSyntaxError` |
| `Lexical(...)` / `Semantic(...)` appear before the last op | `QuailSyntaxError` |
| Invalid comparison operand or ranking arithmetic | `QuailSyntaxError` |
| Evaluated comparison values cannot be ordered | `QuailRuntimeError` |

**Comparisons** — each yields a `Predicate` (not a Python `bool`):

```python
(self == other) -> Predicate
(self != other) -> Predicate
(self <  other) -> Predicate
(self <= other) -> Predicate
(self >  other) -> Predicate
(self >= other) -> Predicate
```

| Operator | Right operand |
| --- | --- |
| `==` `!=` | A `JSONLike` value, or another `Expression`. `is` is rejected at parse — write `== None` / `!= None`. |
| `<` `<=` `>` `>=` | A finite numeric literal (not `bool`, not `inf`/`nan`), or another `Expression`. Expression-to-Expression ordering is accepted at construction and fails at evaluation unless both non-missing results are numeric. |

**Ranking arithmetic** — an Expression used with `+` / `*` becomes a `Ranking`.
Every `Expression` operand must be rankable (see `Ranking`):

```python
(self + other: Expression | Ranking) -> Ranking
(self * weight: int | float) -> Ranking   # weight on the right; finite, not bool, >= 0
```

Invalid ranking operands and weights raise `QuailSyntaxError`; see
[`Ranking`](#ranking).

**Forbidden**

- Python `and` / `or` / `not`, `if` / `while` on an Expression, or chained
  comparisons (`a < expr < b`).
- Iterating an Expression.

---

### `Predicate`

```python
class Predicate:
    def __init__(self, left: Any, operator: str, right: Any = None) -> None: ...
```

True/false recipe per entry. The usual path is comparing an `Expression`, or
composing with `&` `|` `~` — those operators call this constructor. Direct
construction is allowed when the operands match the operator:

| `operator` | `left` | `right` |
| --- | --- | --- |
| `"=="` `"!="` `"<"` `"<="` `">"` `">="` | `Expression` | Same as [Expression comparisons](#expression) |
| `"and"` `"or"` | `Predicate` | `Predicate` |
| `"not"` | `Predicate` | omit the argument, or pass `None` |

Anything else, including a missing `left`/`operator`, is not a valid
`Predicate` (wrong operands → `QuailSyntaxError`; omitted required arguments
originate as Python `TypeError` and are reported to the tool caller as
`QuailRuntimeError`).

**Operators**

```python
(self & other: Predicate) -> Predicate    # both
(self | other: Predicate) -> Predicate    # either
(~self) -> Predicate                      # not
```

**Example**

```python
pred = Expression(Field("body"), Length()) >= 500
mentions = Expression(Field("body"), RegexSearch("hydrangea")) != None
both = pred & mentions
either = pred | mentions
not_pred = ~pred
group = G0.where(pred)
```

**Forbidden:** Python `and` / `or` / `not`; `if` / `while` on a Predicate;
`+` on a Predicate. Compose with `&` `|` `~`, then select entries with
`G0.where(...)`.

---

### `GroupExpr`

```python
class GroupExpr:
    scope: GroupScope

    def __init__(
        self,
        scope: GroupScope,
        predicate: Predicate | None = None,
        members: list[Entry] | list[Field] | None = None,
        name: str | None = None,
    ) -> None: ...

    def where(self, predicate: Predicate) -> GroupExpr: ...

G0: GroupExpr  # all entries (import order)
G1: GroupExpr  # all fields (source then analysis)
```

Symbolic population of entries or fields. Not a Python list — pass it to
`retrieve` / `count`. Do not iterate, index, or use in `if` / `while`.

One constructor call takes exactly one of `predicate`, `members`, or `name`.
`&` `|` `~` combine finished groups; they are not constructor arguments:

| Form | Call | Meaning |
| --- | --- | --- |
| Named builtin | `GroupExpr("entries", name="G0")` / `GroupExpr("fields", name="G1")` | The injected `G0` / `G1`. |
| Filtered | `GroupExpr("entries", predicate=pred)` | Entries matching `pred`. |
| Members | `GroupExpr("entries", members=[entry])` or `GroupExpr("fields", members=[field])` | Explicit `Entry` or `Field` handles matching `scope`, in the given order. |
| Composition | `group_a & group_b`, `group_a \| group_b`, `~group` | Intersection, union, complement. Same `scope` on both sides. |

Intersection keeps the left group's order. Union is the left group, then
previously unseen right-side members. Complement is relative to `G0` (entries)
or `G1` (fields), in that universe's order.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `scope` | `"entries"` \| `"fields"` | required | What the group contains. |
| `predicate` | `Predicate` \| `None` | `None` | Entry filter. Invalid on field groups. |
| `members` | `list[Entry]` \| `list[Field]` \| `None` | `None` | Must be a `list`. An empty list is a legal empty group. Entry groups take `Entry` handles; field groups take `Field` references. |
| `name` | `str` \| `None` | `None` | Only `"G0"` with `"entries"`, or `"G1"` with `"fields"`. |

**Errors**

| When | Error |
| --- | --- |
| `scope` is not `"entries"` or `"fields"` | `QuailSyntaxError` |
| No form, or more than one form | `QuailSyntaxError` |
| `predicate` on a field group | `QuailScopeError` |
| `members` is not a `list`, or item types do not match `scope` | `QuailSyntaxError` |
| `predicate` is not a `Predicate` | `QuailSyntaxError` |
| `name` is not the matching builtin | `QuailSyntaxError` |
| Mixed-scope `&` / `\|`, or a non-`GroupExpr` operand | `QuailScopeError` |

**Operators** — `&` and `|` require the same `scope` on both sides:

```python
(self & other: GroupExpr) -> GroupExpr
(self | other: GroupExpr) -> GroupExpr
(~self) -> GroupExpr
```

#### `GroupExpr.where`

```python
group.where(predicate: Predicate) -> GroupExpr
```

Entry-scoped only. Equivalent to `group & GroupExpr("entries", predicate=predicate)`.

| Name | Type | Description |
| --- | --- | --- |
| `predicate` | `Predicate` | From comparing an `Expression`, or from `&` / `\|` / `~`. |

**Errors:** `QuailScopeError` if `group` is not entry-scoped;
`QuailSyntaxError` if `predicate` is not a `Predicate`.

---

### `Ranking`

```python
class Ranking:
    def __init__(self, expression: Expression | None = None) -> None: ...
```

How `retrieve` orders entry-scoped candidates. An empty ranking keeps the
group's own order: import order for `G0`, source-then-analysis for `G1`,
supplied order for `members` groups, left-then-unseen-right for union.
Non-empty = score each candidate, **higher first**. Ties break by dataset
import order. A missing (`None`) or non-finite score sorts last, as `-inf`.
`AsNumber` and `Semantic` produce `None` for an absent cell. `Length` and
`Lexical` never go missing: absence is the finite score `0` / `0.0`, which
sorts by value. In a sum, any `-inf` term makes the total `-inf`.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `expression` | `Expression` \| `None` | `None` | A **single** rankable Expression. Combined Rankings are already Rankings — do not wrap them again. |

A rankable Expression is one whose final pipeline kind is `number` or
`score`. An `Expression` ending in `Lexical(...)` or `Semantic(...)` is
an ordinary score expression — use it in predicates or as a `retrieve` unit;
wrap it in `Ranking(expression=...)` only for ordered retrieval.

**Operators**

```python
(self + other: Ranking | Expression) -> Ranking
(self * weight: int | float) -> Ranking   # weight on the right
```

| Rule | Detail |
| --- | --- |
| `+` | Sum of rankable scores. An Expression on either side is treated as `Ranking(expression=...)`. Adding an empty Ranking is a no-op. |
| `*` | Scale by `weight`. `weight` must be a finite `int` or `float` `>= 0` (not `bool`), and must be on the **right** (`expr * 0.5`, not `0.5 * expr`). Cannot weight an empty Ranking. |

**Example**

```python
matching = G0.where(Expression(Field("body"), Length()) >= 500)
score_a = Expression(Field("body"), Length())
score_b = Expression(Field("body"), Lexical("hydrangea"))
rank = score_a + score_b * 0.5
# or: Ranking(expression=score_a) + Ranking(expression=score_b) * 0.5
ranked = retrieve(group=matching, rank=rank, limit=10)
```

For a single-expression ranking, pair handles with scores by retrieving
`unit=entries` and `unit=expression` with identical `group`, `rank`, `order`,
and `limit`. Combined ranking totals cannot be retrieved as a unit. Mismatched
arguments are not aligned.

**Errors**

| When | Error |
| --- | --- |
| `expression` is set but is not a rankable `Expression` | `QuailSyntaxError` |
| Weight is not a finite non-negative `int` or `float`; `bool` is always invalid; weight is on the left | `QuailSyntaxError` |
| Weighting an empty `Ranking()` | `QuailSyntaxError` |
| Addition receives an unsupported operand or a non-rankable `Expression` | `QuailSyntaxError` |

---

## Operations

Each op is a **factory**: call it, pass the result to `Expression`.
Pipelines are checked at construction.

### Pipeline kinds

Pipeline kinds describe non-`None` values. Unless an operation says otherwise,
absence may still propagate as `None`.

| Kind | Meaning |
| --- | --- |
| `any` | Unread field value. |
| `text` | One string. |
| `number` | One finite number (`int` or `float`, not `bool`). `Length()` materializes an `int`; `AsNumber()` materializes a `float`. |
| `list_text` | `list[str]`. |
| `text_or_list` | `str` or `list[str]`, proven at runtime. |
| `score` | Search relevance score; **ends the pipeline**. |

**Rule:** each op must accept the kind the previous op produced. Rankable
expressions are those whose final kind is `number` or `score`. Construction
acceptance is not the same as runtime cell type: `any` / `text_or_list` mean
the pipeline is legal, not that every cell will succeed.

Before `RegexSearch` or `RegexFindAll`, use `AsText()` if the cell may be a
list or another non-string value. `RegexSub` and `Slice` also accept
`list[str]`. Do **not** insert `AsText()` only to "prepare" a search op —
transforming ops before `Lexical` / `Semantic` skip the warm source index and
score the pipeline output instead. Identity `Value()` does not.

| Op | Accepts | Produces |
| --- | --- | --- |
| `Value()` | `any` (first position only) | unchanged |
| `AsText()` | `any`, `text`, `number`, `list_text`, `text_or_list` | `text` |
| `AsNumber()` | `any`, `text`, `number`, `text_or_list` | `number` |
| `RegexSearch(...)` | `any`, `text`, `text_or_list` | `text` |
| `RegexFindAll(...)` | `any`, `text`, `text_or_list` | `list_text` |
| `RegexSub(...)` | `any`, `text`, `list_text`, `text_or_list` | input kind (`any` → `text_or_list`) |
| `Slice(...)` | `any`, `text`, `list_text`, `text_or_list` | input kind (`any` → `text_or_list`) |
| `Length()` | `any`, `text`, `list_text`, `text_or_list` | `number` |
| `Lexical(...)` | `any`, `text`, `list_text`, `text_or_list` | `score` (terminal) |
| `Semantic(...)` | `any`, `text`, `list_text`, `text_or_list` | `score` (terminal) |

Regex uses a bounded RE2-style engine (not Python backtracking). No lookaround,
no backreferences. Each UTF-8-encoded pattern and replacement is capped at
16 KiB (`QuailSyntaxError` if exceeded). Supported flags via `re`: `I`, `M`,
`S` only (`re.A` / `re.U` are rejected — word classes are ASCII). Combine
flags with `|`.

---

### `Value`

```python
Value() -> Operation
```

Identity. Optional; if present, it must be the first operation.

**Accepts:** `any` (first position only). **Produces:** unchanged.

---

### `AsText`

```python
AsText() -> Operation
```

`""` if the value is `None`, otherwise Python `str(value)`. That means a later
`Semantic(...)` scores the empty string instead of `None`, and a later
`Lexical(...)` FTS-scores an empty segment instead of the absent-cell `0.0`
short-circuit.

**Accepts:** `any`, `text`, `number`, `list_text`, `text_or_list`. **Produces:** `text`.

---

### `AsNumber`

```python
AsNumber() -> Operation
```

Finite `float` from an `int`, `float`, or numeric string. `None` stays `None`.
`bool`, lists, dictionaries, non-numeric text, and non-finite numbers raise
`QuailRuntimeError` at evaluation.

**Accepts:** `any`, `text`, `number`, `text_or_list`. **Produces:** `number`.

---

### `RegexSearch`

```python
RegexSearch(pattern: str, flags: int = 0) -> Operation
```

The first matching substring (`group(0)`, not capturing groups), or `None` if
there is no match or the input cell is `None`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `pattern` | `str` | required | RE2-style pattern. |
| `flags` | `int` | `0` | `re.I`, `re.M`, `re.S` only; combine with `\|`. |

**Accepts:** `any`, `text`, `text_or_list` at construction. **Produces:** `text`.
At evaluation the cell must be `str` (or `None`); a list raises
`QuailRuntimeError` (`use AsText() first`).

Invalid argument types, unsupported flags, and patterns larger than 16 KiB
UTF-8 raise `QuailSyntaxError`.

---

### `RegexFindAll`

```python
RegexFindAll(pattern: str, flags: int = 0) -> Operation
```

All matches as `list[str]` of full-match text (`group(0)`). `None` input or
no match yields `[]`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `pattern` | `str` | required | RE2-style pattern. |
| `flags` | `int` | `0` | `re.I`, `re.M`, `re.S` only; combine with `\|`. |

**Accepts:** `any`, `text`, `text_or_list` at construction. **Produces:** `list_text`.
At evaluation the cell must be `str` (or `None`); a list raises
`QuailRuntimeError` (`use AsText() first`).

Invalid argument types, unsupported flags, and patterns larger than 16 KiB
UTF-8 raise `QuailSyntaxError`.

---

### `RegexSub`

```python
RegexSub(pattern: str, replacement: str, flags: int = 0) -> Operation
```

Replaces regex matches with literal replacement text. The replacement is not a
backreference template. `None` input stays `None`. For `list[str]` input, the
substitution is applied independently to each string.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `pattern` | `str` | required | RE2-style pattern. |
| `replacement` | `str` | required | Literal replacement text. |
| `flags` | `int` | `0` | `re.I`, `re.M`, `re.S` only; combine with `\|`. |

**Accepts:** `any`, `text`, `list_text`, `text_or_list`.
**Produces:** the input kind (`any` → `text_or_list`).

Invalid construction arguments raise `QuailSyntaxError`. A runtime value other
than `str`, `list[str]`, or `None` raises `QuailRuntimeError`.

---

### `Slice`

```python
Slice(start: int, end: int | None = None) -> Operation
```

Python slice `[start:end]` on text **or** list. `None` input stays `None`.
For `list[str]` input, the slice is over the list, not inside each string.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `start` | `int` | required | Slice start (not `bool`). |
| `end` | `int` \| `None` | `None` | Slice end (not `bool`); `None` means through the last item. |

**Accepts:** `any`, `text`, `list_text`, `text_or_list`.
**Produces:** the input kind (`any` → `text_or_list`).

A non-`int` bound, including `bool`, raises `QuailSyntaxError`. A runtime
value other than `str`, `list`, or `None` raises `QuailRuntimeError`.

---

### `Length`

```python
Length() -> Operation
```

`len(text)`, `len(list)`, or `0` for `None`. Rankable (`number`). Materializes
an `int`. Any non-`None` runtime value other than `str` or `list` raises
`QuailRuntimeError` at evaluation (including numbers).

**Accepts:** `any`, `text`, `list_text`, `text_or_list`. **Produces:** `number`.

---

### `Lexical`

```python
Lexical(
    query: Query,
    input_aggregation: Aggregation = None,
    target_aggregation: Aggregation = None,
) -> Operation
```

FTS relevance score. Terminal — must end the pipeline. Rankable (`score`).
Ordinary score expression: usable in predicates and as a `retrieve` unit.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | `str` \| `list[str]` \| `GroupExpr` \| `list[Entry]` | required | A target source; must resolve to at least one non-empty target string. |
| `input_aggregation` | `"total"` \| `"avg"` \| `None` | `None` | How to combine scores across input text segments for one entry. `None` = `"total"`. `"avg"` divides by all input segments, including unmatched ones. |
| `target_aggregation` | `"total"` \| `"avg"` \| `None` | `None` | How to combine scores across targets. `None` = `"total"`. |

**Query shapes** — every shape must resolve to one or more non-empty target
texts.

| Spell as | Meaning |
| --- | --- |
| `str` | One target. FTS query syntax (below). Must be non-empty. |
| `list[str]` | Each string is its own query. Must contain at least one non-empty string. Target aggregation **sums** those scores (`None` / `"total"`) or takes their **mean** (`"avg"`). That is not the same as unquoted spaces inside one string (those are FTS OR). |
| Entry-scoped `GroupExpr` | For each member, read the **root `Field` of the surrounding `Expression`** and use that cell as a target. |
| `list[Entry]` | Same: read the surrounding `Expression`'s root field from each listed `Entry`. |

A `list[str]` cell on a target Entry expands to one target per non-empty
element.

**FTS syntax** (string queries)

| Syntax | Meaning |
| --- | --- |
| Unquoted terms separated by spaces | OR (not a phrase) |
| `"quoted text"` | Adjacent tokens. Quotes have no escape syntax. |
| `term*` | Prefix of one punctuation-free term. `*` is allowed once, only at the end. Bare `*` is an error. |
| Uppercase `AND` / `NOT` | Operators. `NOT` is infix and requires a positive left operand (`rose NOT soil`, not `NOT spam`). |
| Uppercase `OR` | Error. Separate terms with spaces. |
| Lowercase `and` / `not` / `or` | Ordinary terms |
| Punctuation | One unquoted atom with hyphens/punctuation becomes OR of the split tokens |

A whitespace-only or punctuation-only string query raises `QuailRuntimeError`
because it contains no FTS terms.

Entry-derived targets tokenize and quote their terms so prose like `AND` is
not an FTS operator.

**Scoring:** `score > 0` means "matched". Unmatched and absent cells score
`0.0` (not `None`). Scores are corpus-relative.

**Accepts:** `any`, `text`, `list_text`, `text_or_list`. **Produces:** `score`.

**Errors:** `QuailSyntaxError` if the query is the wrong shape, an empty
`str` / `list[str]` / `list[Entry]`, or an aggregation is not `"total"` /
`"avg"` / `None`. `QuailScopeError` if a `GroupExpr` query is not
entry-scoped, or a query `Entry` belongs to another dataset or version.
`QuailRuntimeError` if resolved entry targets have no non-empty text, if a
string query has no FTS terms, or if search is not configured (repairable —
the dataset operator must configure search, then retry the whole exec).

---

### `Semantic`

```python
Semantic(
    query: Query,
    input_aggregation: Aggregation = None,
    target_aggregation: Aggregation = None,
) -> Operation
```

Exact cosine similarity under the dataset embedding profile (configured
outside this API). Terminal. Rankable (`score`). Ordinary score expression:
usable in predicates and as a `retrieve` unit. Same [query shapes and
aggregation rules](#lexical) as `Lexical`.

**Scoring:** cosine is **not** a match bit. Do not reuse Lexical's
`score > 0` as "matched". Absent input cells score `None` (Lexical absence is
`0.0`). An empty string is embedded and scored as text. Larger scores indicate
greater similarity. Aggregated (`"total"`) scores are sums, not unit cosines.

**Accepts:** `any`, `text`, `list_text`, `text_or_list`. **Produces:** `score`.

**Errors:** construction-shape and aggregation errors raise `QuailSyntaxError`.
Non-entry query groups and out-of-scope entries raise `QuailScopeError`.
Missing search configuration or empty resolved entry targets raise
`QuailRuntimeError`. FTS parse failures (whitespace-only or punctuation-only
string queries, uppercase `OR`, pure-negative `NOT`) are **Lexical only** —
Semantic still embeds that string.

---

### Search performance

Both `Lexical` and `Semantic` run fastest on a **bare source field** (the
search op is the only transforming op, field `kind` is `"source"`, and that
field was processed for search). Identity `Value()` before the search op does
not skip that index. Transforming prefix ops skip it and score the pipeline
output instead. Analysis fields load and score cell values. Warm paths are
optimizations; they do not change the recipe. Lexical scores are corpus-relative.

`quail_export_csv` is the host route to treat session analysis columns as
source columns after the operator processes the export — see
[Host tool: `quail_export_csv`](#host-tool-quail_export_csv).

---

## Functions

These names are injected. They are not methods on a type.

---

### `retrieve`

```python
retrieve(
    unit: Unit | Expression = entries,
    group: GroupExpr = G0,
    limit: int = 1,
    order: Order = "top",
    rank: Ranking | None = None,
) -> list[Any]
```

Materialize a bounded list from a symbolic group.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `unit` | `Unit` \| `Expression` | `entries` | What each list item is. An Expression yields one computed value per entry that remains after ranking and `limit`. Not a `GroupExpr` or a string. |
| `group` | `GroupExpr` | `G0` | Population to draw from. `Unit("fields")` needs a fields group; every other unit needs an entries group. |
| `limit` | `int` | `1` | Positive `int` (`bool` is invalid). Omitted `limit` is **1**, not the whole group. `limit` must be at least 1, so do not pass `limit=count(...)` when the group is empty. |
| `order` | `"top"` \| `"middle"` \| `"bottom"` | `"top"` | Slice of the ordered candidate sequence. If `limit >= len(items)`, all items are returned. `"top"` is the prefix `items[:limit]`. `"bottom"` is the suffix `items[-limit:]` (order preserved, not reversed). `"middle"` is a centered window `start = (len(items) - limit) // 2`. |
| `rank` | `Ranking` \| `None` | `None` | Ordering. `None` is normalized to `Ranking()`. Empty ranking keeps the group's own order. |

**Returns:** always a `list` (possibly empty). Item type depends on `unit`:

| `unit` | `group` scope | Items | Can rank? |
| --- | --- | --- | --- |
| `entries` / `Unit("entries")` | entries | `Entry` | yes |
| `fields` / `Unit("fields")` | fields | `Field` | no |
| `Unit("entries", field)` | entries | present values | yes (absence is dropped **before** ranking) |
| `Unit("values", field)` | entries | distinct values (full group, then `limit` / `order`) | no |
| `Expression` | entries | computed values | yes |

**Notes**

- Narrow with `.where` **before** expensive ranking when the predicate does
  not depend on the ranking result. Ranking scores the whole candidate set
  (after present-value filtering, if any) before applying `limit`.
- `fields` and `values` units cannot take a **non-empty** ranking
  (`QuailScopeError`). The default empty `Ranking()` is allowed.
- An Expression unit is evaluated **after** ranking and `limit`.

**Errors:** `QuailSyntaxError` for an invalid `unit`, `group`, `limit`,
`order`, or `rank` type or value; `QuailScopeError` for unit/group mismatch,
ranking a non-rankable unit, or out-of-scope member `Entry`s;
`QuailFieldError` if a unit `Field` is unknown or kind-mismatched;
`QuailRuntimeError` if materializing an Expression fails (invalid cell data,
search unavailable, timeout, or another resource limit).

---

### `count`

```python
count(
    unit: Unit | Expression = entries,
    group: GroupExpr = G0,
) -> int
```

Size of the population `retrieve` would draw from (no `limit` / `order` /
`rank`).

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `unit` | `Unit` \| `Expression` | `entries` | Same legal pairs as `retrieve`. |
| `group` | `GroupExpr` | `G0` | Population. `Unit("fields")` needs a fields group; every other unit needs an entries group. |

**Returns:** `int`. For `Unit("values", field)`, the number of distinct present
values over the full group. For `Unit("entries", field)`, the number of entries
where that field is present. For an `Expression` unit, `count` returns the
number of entry IDs in the group — the unit expression is **not** run (so a
Lexical unit does not require search to be configured, unlike `retrieve`).

**Errors:** `QuailSyntaxError` for invalid argument shapes; `QuailScopeError`
for unit/group mismatch or out-of-scope members; `QuailFieldError` for unknown
or kind-mismatched unit fields. The unit expression is not evaluated, so its
runtime failures do not occur. Expressions used by `group` are still evaluated
and can fail.

---

### `create_field`

```python
create_field(field: str | Field) -> Field
```

Create a session-only analysis column. Source fields cannot be created or
overwritten.

| Name | Type | Description |
| --- | --- | --- |
| `field` | `str` \| `Field` | Column name, `Field("topic")`, or `Field("topic", "analysis")`. The name is stripped; empty after strip is illegal. |

**Returns:** `Field(stripped_name, kind="analysis")`. If the analysis field
already exists, no write occurs; the function still returns that normalized
`Field`. Use the returned handle for later `tag` / `untag` (a `Field` you
construct yourself does not strip whitespace).

**Errors:** `QuailSyntaxError` if the name is empty after strip, the argument
is the wrong type, or `Field.kind` is `"source"`. `QuailFieldError` if the
name collides with a source field.

---

### `tag`

```python
tag(
    group: GroupExpr | list[Entry],
    field: Field,
    value: TagValue,
) -> None
```

Write `value` to `field` for every selected entry, replacing any value already
there. Session overlay only.

| Name | Type | Description |
| --- | --- | --- |
| `group` | `GroupExpr` \| `list[Entry]` | Entry-scoped group, or a list of `Entry` handles. Prefer the group. `retrieve` defaults to `limit=1`, so `tag(retrieve(group=matching), ...)` tags at most one entry. An empty list or empty group writes nothing; `field` is still resolved (unknown/source field still errors). |
| `field` | `Field` | An existing analysis field. Call `create_field` first; tagging an unknown field is a `QuailFieldError`. |
| `value` | `TagValue` | Recursively: `bool`, `int`, finite `float`, `str`, list, or string-keyed dictionary. **No `None` anywhere**, no cycles, no non-finite floats. |

**Returns:** `None`.

**Errors:** `QuailSyntaxError` if `field` is not a `Field`, `value` is `None` or
not a `TagValue`, or `group` is the wrong shape. `QuailScopeError` if `group`
is not entry-scoped, or a listed `Entry` belongs to another dataset or
version. `QuailFieldError` if `field` is source or unknown.
`QuailRuntimeError` if evaluating the selected group fails.

---

### `untag`

```python
untag(
    group: GroupExpr | list[Entry],
    field: Field,
    value: TagValue | None = None,
) -> None
```

Clear analysis tags on the selection.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `group` | `GroupExpr` \| `list[Entry]` | required | Same as `tag`. Empty selection writes nothing; `field` is still resolved. |
| `field` | `Field` | required | Analysis field to clear. |
| `value` | `TagValue` \| `None` | `None` | `None` clears all selected present cells. A value clears cells where Python `==` matches, so `True` also matches `1` and `1` also matches `1.0`. |

**Returns:** `None`.

**Errors:** `QuailSyntaxError` for an invalid `group`, `field`, or non-`None`
value that is not a `TagValue`. `QuailScopeError` for a non-entry group or
out-of-scope `Entry`. `QuailFieldError` for an unknown or source field.
`QuailRuntimeError` if group evaluation fails.

---

### `print`

```python
print(*values: Any, sep: str = " ", end: str = "\n") -> None
```

Append to the exec print buffer. That buffer is the **only** caller-visible
analysis output (`printed_output` on success).

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `*values` | `Any` | — | Converted with `str(...)`. |
| `sep` | `str` | `" "` | Between values. |
| `end` | `str` | `"\n"` | After the last value. |

**Returns:** `None`. The UTF-8-encoded buffer is capped at 1 MiB
(`QuailRuntimeError` if exceeded). Failed execs discard the buffer.

For concise, stable output, print scalars such as `entry.id`, `field.name`,
`field.kind`, or cell values. Do not treat the string form of Quail objects
as a stable serialization.

---

## Helpers

### `re`

```python
re.I = re.IGNORECASE
re.M = re.MULTILINE
re.S = re.DOTALL
re.NOFLAG  # accepted zero
re.A = re.ASCII      # exists; regex factories reject it
re.U = re.UNICODE    # exists; regex factories reject it

re.escape(pattern: str) -> str
```

Injected regex helper — **not** Python's `re` module. Flags and `escape`
only. Pass flags into `RegexSearch` / `RegexFindAll` / `RegexSub`.

| Name | Meaning |
| --- | --- |
| `re.I` | Ignore case. Also `re.IGNORECASE`. |
| `re.M` | Multiline. Also `re.MULTILINE`. |
| `re.S` | Dot matches newline. Also `re.DOTALL`. |
| `re.escape(pattern)` | Escape a literal string for use in a pattern. `pattern` must be `str`; otherwise `QuailSyntaxError`. |

`re.A` / `re.U` exist as attributes but are **rejected** as regex flags
(RE2 word classes are ASCII).

---

## Errors

Diagnostics name these exception classes. Sandbox code cannot catch them
(`try` is rejected). Read the diagnostic, fix the code, rerun the whole call.

```python
class QuailError(Exception): ...
class QuailSyntaxError(QuailError): ...
class QuailScopeError(QuailError): ...
class QuailFieldError(QuailError): ...
class QuailRuntimeError(QuailError): ...
```

| Class | Cause category |
| --- | --- |
| `QuailSyntaxError` | Bad API shape, illegal Python construct, or bad symbolic combo. |
| `QuailScopeError` | Wrong group, unit, session, or version pairing. |
| `QuailFieldError` | Unknown field, kind mismatch, or source mutation. |
| `QuailRuntimeError` | Bad data for an op, search down, timeout, resource limit, unpersistable binding, `session_busy`, or `server_busy`. |

Failures are atomic: no overlay writes, no bindings, no printed text.

A failed `quail_exec` returns the diagnostic object documented under
[How you call Quail](#how-you-call-quail). Host failures are not limited to
the four concrete `QuailError` subclasses listed above; follow
`stable_error_code` and `repair_hint`.

---

## Bindings

After a **successful** exec, every top-level name that still exists is restored
on the next exec in the **same session**, provided its final value is
persistable. Delete with `del name` if it should not persist. A name may be
rebound during execution; only its **final** value is encoded.

Every surviving top-level name is committed, including loop targets and
temporary iterators. If that final value cannot persist, the whole exec fails
(`QuailRuntimeError`) — nothing commits. `enumerate(...)` and `range(...)`
objects cannot persist; `del numbered` before the exec ends if you used them.

Binding names are at most 128 UTF-8 bytes. Literal values must be acyclic and
finite: nesting depth at most 64, at most 100,000 aggregate items, integers
under 100 decimal digits.

| Values that persist | Values that fail the execution |
| --- | --- |
| JSON-like scalars (including `None`), lists, and string-keyed dictionaries of JSON-like values — finite floats, no cycles | tuples, sets, callables, `re`, iterators, `range` objects, non-finite floats, cycles, non-string dict keys |
| Quail objects: `Field`, `Unit`, `Operation` instances (e.g. `Length()`), `Expression`, `Predicate`, `GroupExpr`, `Ranking`, `Entry` | Op **factories** (`Length` itself) and other callables |
| Lists of JSON-like values **and** lists of those Quail objects (including `retrieve` results; a list may mix JSON-like values and listed Quail object types) | Dicts whose values are Quail objects (no list-style fallback). Dictionary values must be JSON-like at every depth. |

Analysis tags remain scoped to the session + dataset version. Bindings are
session-scoped. A persisted `Entry` remains bound; using it against another
dataset or dataset version raises `QuailScopeError`. A persisted `Field` may
raise `QuailFieldError` against another dataset.

---

## Python surface (bounded)

Allowed: literals; simple assignment (not annotated assignment, assignment
expressions, or any augmented assignment such as `+=`); `del name`; `if` /
`elif` / `else` / `for` / `while` / `break` / `continue` / `pass` on
**materialized** Python values (booleans, numbers, strings, lists, tuples,
sets, dictionaries, and `Entry` / `Field` handles); Python `and` / `or` /
`not` on those materialized values; read-only indexing and slicing of
materialized lists, tuples, dictionaries, and strings; membership (`in`);
arithmetic and bitwise operators on materialized numbers; the operators
documented for Quail symbolic types; calls to the injected API; reads of
attributes documented in this API; `entry.value` / `entry.fields` /
`group.where` / `re.escape`; and these string methods, called directly on a
receiver:

`startswith`, `endswith`, `lower`, `upper`, `casefold`, `strip`, `lstrip`,
`rstrip`, `replace`, `split`, `rsplit`, `splitlines`, `count` (the `str`
method, distinct from the injected `count()`), `find`, `rfind`,
`removeprefix`, `removesuffix`.

Rebuild and rebind instead of mutating containers:

```python
ids = []
for entry in retrieve(limit=20):
    ids = ids + [entry.id]
```

Iterate a dict directly (`for key in mapping:`) and subscript `mapping[key]`.
`.items()`, `.keys()`, `.values()`, and `.get()` are unavailable.

Not allowed (`QuailSyntaxError` at parse): imports, `def` / `lambda` /
`class`, comprehensions, f-strings, `try` / `except`, `raise` / `assert` /
`with`, `match`, `async` / `await` / `yield`, `global` / `nonlocal`, `is`,
`open` / `eval` / `exec` / `compile` / `__import__`, mutating methods on
containers, item or attribute assignment/deletion (`xs[0] = …`, `obj.x = …` —
rebind the **name** instead), unlisted methods (including `str.join`,
`str.format`, `dict.get`, `dict.items`, `dict.keys`, and `dict.values`),
`re.compile`, bound-method loads of approved methods (they must be called
directly), and any operation not listed as allowed.

---

## Host tool: `quail_export_csv`

`quail_export_csv` is a **host MCP tool**, not a name inside `quail_exec`.

```text
quail_export_csv(session_id, dataset_id)
```

Pass arguments by name. Writes `"id"`, source columns, and this session's
**analysis fields** (including created-but-untagged columns) to a CSV on the
Quail server host.
The tool result is not the file body:

```text
{"path": str, "session_id": str, "dataset_id": str,
 "dataset_version_id": str, "columns": list[str], "row_count": int}
```

`path` is a filesystem path on the Quail server host. `columns` is `"id"`, then
source fields, then analysis fields.

Export does not import the CSV or build search indexes. The dataset operator
may import the exported CSV as a new dataset and process it so those analysis
columns become source columns eligible for warmed bare-field search.

Do not overlap it with `quail_exec` or another `quail_export_csv` on the same
`session_id` (stable code `session_busy`).
