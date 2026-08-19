# Quail Analysis API

Quail lets you analyze a private dataset by writing bounded Python. You build
**symbolic** recipes (filters, scores, groups), ask Quail to **materialize**
bounded results, **tag** session-only analysis labels, and **print** to receive information back as the result. Imported source data never changes.

---

## How you call Quail

```text
quail_exec(session_id, dataset_id, code, time_window="standard")
```

Pass arguments by name.

| Argument | Meaning |
| --- | --- |
| `session_id` | Durable analysis context in one workspace. Bindings and tags stick to this session. MCP binds the workspace; if the workspace changes, start a new session. Run one `quail_exec` at a time per `session_id` — overlap (including `quail_export_csv` on the same session) fails with `session_busy`. |
| `dataset_id` | Exactly one dataset for this call (its active immutable version). |
| `code` | Bounded Quail Python (no imports, no files, no network). |
| `time_window` | `"standard"` (30s wall / 15s CPU) or `"extended"` (100s wall / 60s CPU). Both are finite; extended is just longer. Worker RSS is always capped at 256 MiB. |

**Success:** `{"printed_output": "<exactly what print() wrote>"}`.  
**Failure:** a tool error with `execution_id` (or `null`) and a `diagnostic`.
Nothing partial is kept if quail_exec fails — no tags, no bindings, no printed text.

Only `print(...)` leaves the sandbox. Return values of expressions do not.

---

## Vocabulary

| Term | Meaning |
| --- | --- |
| **Entry** | One row in the dataset. |
| **Field** | One source or analysis column. The CSV `id` column is `entry.id`, not `Field("id")`. |
| **Expression** | Recipe that reads/transforms one field’s value per entry. |
| **Predicate** | True/false recipe per entry (usually from comparing expressions). |
| **Group** | Symbolic set of entries or fields — not a Python list until you retrieve. |
| **Unit** | What `retrieve`/`count` should return (entries, fields, values, …). |
| **Ranking** | How to score and order entries. |
| **Binding** | Top-level name that survives a successful exec in this session. |
| **Mutation** | `create_field` / `tag` / `untag` — session overlay only. |

**Symbolic vs materialized:** building `Expression(...)` or `G0.where(...)`
does not read the data. Quail evaluates when you `retrieve`, `count`,
`entry.value`, `tag`, etc.

---

## First exec

Field names differ per dataset — inspect before assuming any:

```python
for field in retrieve(unit=fields, group=G1, limit=50):
    print(field.name, field.kind)

samples = retrieve(limit=1)
if len(samples) > 0:
    for field in samples[0].fields():
        print(field.name, repr(samples[0].value(field)))
```

Empty cells are `None`, not `""`.

From here, follow your question. The pieces below are designed for you to
explore in any direction.

---

## What you can use (injected namespace)

No imports. These names are injected and reserved.

| Kind | Names |
| --- | --- |
| Callables | `retrieve`, `count`, `create_field`, `tag`, `untag`, `print` |
| Groups / units | `G0`, `G1`, `entries`, `fields` |
| Types | `Field`, `Unit`, `Expression`, `Predicate`, `GroupExpr`, `Ranking`, `Entry` |
| Ops | `Value`, `AsText`, `AsNumber`, `RegexSearch`, `RegexFindAll`, `RegexSub`, `Slice`, `Length`, `Lexical`, `Semantic` |
| Regex helper | `re` (flags + `re.escape` only — not Python’s `re` module) |
| Errors | `QuailError`, `QuailSyntaxError`, `QuailScopeError`, `QuailFieldError`, `QuailRuntimeError` |
| Safe builtins | `abs`, `all`, `any`, `bool`, `dict`, `enumerate`, `float`, `int`, `len`, `list`, `max`, `min`, `range`, `repr`, `round`, `set`, `str`, `sum`, `tuple` |

- **`G0`**: all entries (import order). **`G1`**: all fields (source then analysis).
- **`entries` / `fields`**: default units for retrieve/count — not groups.

Compose symbolic values with `&` `|` `~` and comparisons. Do **not** use Python
`and` / `or` / `not`, `if` on a Predicate, or chained comparisons on Expressions.
Use `== None` / `!= None`, not `is None`.

---

## Core rules (do not violate these)

1. **One dataset per exec** — the active immutable version of `dataset_id`.
2. **Source data is frozen** — only analysis fields/tags change, and only in-session.
3. **Print-only output** — success returns the print buffer; failure returns none of it.
4. **Atomic exec** — all tags/bindings/prints commit together or not at all.
5. **Later lines see earlier tags** in the same successful run; failed runs roll back.
6. **No outside world** — no imports, files, network, DB handles, or env.

The `time_window` ceilings are fixed product limits. Hitting any ceiling fails
the whole exec atomically (no tags, bindings, or printed output).

---

## Types

`Field`, `Unit`, `Expression`, `Predicate`, `GroupExpr`, and `Ranking` are
**symbolic recipes**. Constructing them does not read data. `Entry` is a
runtime handle issued by `retrieve`, not a recipe you construct.

Evaluation happens at `retrieve`, `count`, `tag`, `untag`, `entry.value`, and
`entry.fields`. Binding commit also checks Field kinds without those verbs.

Each symbol below has a **signature**, then parameters / returns / errors /
notes in the same order. Call constructors and functions by these signatures.
Do not subclass them.

### Shared shapes

```python
FieldKind = "source" | "analysis" | None
GroupScope = "entries" | "fields"
UnitScope = "entries" | "fields" | "values"
Order = "top" | "middle" | "bottom"
Aggregation = "total" | "avg" | None          # None means "total"
# Non-empty: "" / [] / [""] are illegal. GroupExpr must be entry-scoped.
Query = str | list[str] | GroupExpr | list[Entry]
# Acyclic. float means a finite float (not inf/nan).
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

A named source or analysis column. A Field is a **name**, not a cell value.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `str` | required | Non-empty column name. The CSV `id` column is `entry.id`, not `Field("id")`. |
| `kind` | `"source"` \| `"analysis"` \| `None` | `None` | `None` resolves by name at use. An explicit kind must match the catalog when the Field is **used**. Commit of a binding fails only if that name **already exists** in this dataset catalog with a different kind; an unknown name with an explicit kind may still be bound. |

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
| Unknown name at use | `QuailFieldError` |
| Explicit `kind` does not match the catalog at use, or at commit when the name already exists with a different kind | `QuailFieldError` (the error names the registered kind — use it, or omit `kind`) |
| Compared to a non-`Field`, or ordered with `<` `<=` `>` `>=` | `QuailSyntaxError` |

**Notes**

- Two `Field` values compare equal only to each other, by `(name, kind)`.
- Do not compare a Field to a cell value. Read the column with
  `Expression(field, Value())` (or a numeric / search op) and compare that.
- A restored binding with a stale `kind` does not fail the exec. Using that
  Field still raises; `del name` recovers.

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

What `retrieve` / `count` should return. The population is `group=`, not the Unit.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `scope` | `"entries"` \| `"fields"` \| `"values"` | required | Which kind of item comes back. |
| `field` | `Field` \| `None` | `None` | Required for `"values"`. Optional for `"entries"` (present values of that field; absent cells dropped; not distinct). Forbidden for `"fields"`. |

**Legal forms**

| Call | Meaning |
| --- | --- |
| `Unit("entries")` / `entries` | Entry handles. |
| `Unit("entries", field)` | Present values of `field`, one per matching entry (absent cells dropped). |
| `Unit("fields")` / `fields` | Field handles. Use with a field group such as `G1`. |
| `Unit("values", field)` | Distinct present values over the **full** group. `limit` / `order` apply to that distinct sequence, not to entries first. |

**Errors**

| When | Error |
| --- | --- |
| `scope` is not `"entries"`, `"fields"`, or `"values"` | `QuailSyntaxError` |
| `field` is not a `Field` or `None` | `QuailSyntaxError` |
| `Unit("fields", field)` | `QuailSyntaxError` |
| `Unit("values")` with no `field` | `QuailSyntaxError` |

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

#### `Entry.value`

```python
entry.value(field: Field | str, default: Any = None) -> Any
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `field` | `Field` \| `str` | required | Column to read. A string must be non-empty. |
| `default` | `Any` | `None` | Substituted when the stored cell is `None` (CSV blanks are omitted at import, so absence is `None`, not `""`). |

**Returns:** the stored cell value, or `default` when that value is `None`.
Unknown names and kind mismatches raise; they do not use `default`.

**Errors:** `QuailSyntaxError` if `field` is not a `Field` or non-empty `str`;
`QuailFieldError` if the name is unknown or the explicit kind does not match
the catalog; `QuailScopeError` if the handle belongs to another dataset or
dataset version; `QuailRuntimeError` if called outside evaluation.

#### `Entry.fields`

```python
entry.fields() -> list[Field]
```

**Returns:** `Field` handles present on this row (catalog fields whose cell is
not `None`).

**Errors:** `QuailScopeError` if the handle belongs to another dataset or
dataset version; `QuailRuntimeError` if called outside evaluation.

---

### `Expression`

```python
class Expression:
    def __init__(self, input: Field | Expression, *operations: Operation) -> None: ...
```

Recipe that reads one field’s value per entry and pipes it through typed ops.
Construction type-checks the pipeline; it does not read data.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `input` | `Field` \| `Expression` | required | Root field, or an existing expression to extend. |
| `*operations` | `Operation` | see notes | Pipeline steps from the op factories (`Value()`, `Length()`, …). |

The **resulting** pipeline must have at least one op. When `input` is a
`Field`, that means `*operations` is non-empty. When `input` is an
`Expression`, extra ops may be omitted (clone) or appended. `Value()` is the
identity when reading a field as-is — it is **not** required; `Length()`,
`AsText()`, regex, and search ops may be first. If `Value()` appears, it must
be first. If `Lexical` / `Semantic` appear, they must be last.

**Returns:** a new `Expression`.

**Errors**

| When | Error |
| --- | --- |
| `input` is not a `Field` or `Expression` | `QuailSyntaxError` |
| Resulting pipeline is empty | `QuailSyntaxError` |
| An argument is not an `Operation` | `QuailSyntaxError` |
| An op does not accept the previous op’s kind | `QuailSyntaxError` (names both sides) |
| `Value()` appears after the first op | `QuailSyntaxError` |
| `Lexical` / `Semantic` appear before the last op | `QuailSyntaxError` |

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
| `==` `!=` | `None`, a JSON-like literal (finite floats; lists/dicts with string keys; no cycles), or another `Expression`. Use `== None` / `!= None`, not `is None`. |
| `<` `<=` `>` `>=` | A finite numeric literal (not `bool`, not `inf`/`nan`), or another `Expression`. |

**Ranking arithmetic** — an Expression used with `+` / `*` becomes a `Ranking`
(the expression must be rankable; see `Ranking`):

```python
(self + other: Expression | Ranking) -> Ranking
(self * weight: int | float) -> Ranking   # weight on the right; finite, not bool, ≥ 0
```

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
| `"=="` `"!="` `"<"` `"<="` `">"` `">="` | `Expression` | Same as Expression comparisons |
| `"and"` `"or"` | `Predicate` | `Predicate` |
| `"not"` | `Predicate` | must be omitted (`None`) |

Anything else, including a missing `left`/`operator`, is not a valid
`Predicate` (wrong operands → `QuailSyntaxError`; omitted args → `TypeError`).

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

Symbolic set of entries or fields. Not a Python list — materialize with
`retrieve` / `count`. Do not iterate, index, or use in `if` / `while`.

Exactly one population form per constructor call. `&` `|` `~` build a fourth
**composition** form (do not mix it with the others):

| Form | Call | Meaning |
| --- | --- | --- |
| Named builtin | `GroupExpr("entries", name="G0")` / `GroupExpr("fields", name="G1")` | The injected `G0` / `G1`. |
| Filtered | `GroupExpr("entries", predicate=pred)` | Entries matching `pred`. |
| Members | `GroupExpr("entries", members=[...])` or `GroupExpr("fields", members=[...])` | Explicit `Entry` or `Field` handles matching `scope`. |
| Composition | `group_a & group_b`, `group_a \| group_b`, `~group` | Intersection, union, complement. Same `scope` on both sides. |

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `scope` | `"entries"` \| `"fields"` | required | What the group contains. |
| `predicate` | `Predicate` \| `None` | `None` | Entry filter. Invalid on field groups. |
| `members` | `list[Entry]` \| `list[Field]` \| `None` | `None` | Must be a `list`. Entry groups take `Entry` handles; field groups take `Field` references. |
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

**Operators** — both sides must share `scope`:

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
| `predicate` | `Predicate` | From comparing an `Expression`. |

**Errors:** `QuailScopeError` if `group` is not entry-scoped;
`QuailSyntaxError` if `predicate` is not a `Predicate`.

---

### `Ranking`

```python
class Ranking:
    def __init__(self, expression: Expression | None = None) -> None: ...
```

How `retrieve` orders entry-scoped candidates. Empty ranking = processing
order. Non-empty = score each candidate, **higher first**. A missing or
non-finite score sorts as lowest (`-inf`): `AsNumber` / `Semantic` of an
absent cell is `None` → last; `Length` absence is `0`; `Lexical` absence is
`0.0`.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `expression` | `Expression` \| `None` | `None` | A **single** rankable Expression. Combined Rankings are already Rankings — do not wrap them again. |

A rankable Expression ends in `AsNumber()`, `Length()`, `Lexical()`, or
`Semantic()`. `Lexical` / `Semantic` are ordinary score expressions — use them
in predicates or as a `retrieve` unit; wrap them in `Ranking(expression=...)`
only for ordered retrieval.

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
rank = score_a + score_b * 0.5
# or: Ranking(expression=score_a) + Ranking(expression=score_b) * 0.5
ranked = retrieve(group=matching, rank=rank, limit=10)
```

To pull **aligned** entries and scores, pass the same `group`, `rank`, `order`,
and `limit` on both `retrieve` calls. The runtime does not check that pairing.

**Errors**

| When | Error |
| --- | --- |
| `expression` is set but is not a rankable `Expression` | `QuailSyntaxError` |
| Weight is not a finite non-negative `int`/`float` (including `bool`), or is on the left | `QuailSyntaxError` |
| Weighting an empty `Ranking()` | `QuailSyntaxError` |

---

## Operations

Each op is a **factory**: call it, pass the result to `Expression`. Ops are
not classes you subclass. Pipelines are checked at construction.

### Pipeline kinds

| Kind | Meaning |
| --- | --- |
| `any` | Unread field value. |
| `text` | One string. |
| `number` | One finite float. |
| `list_text` | `list[str]`. |
| `text_or_list` | Text or `list[str]`, proven at runtime by a preceding op. |
| `score` | Search relevance score; **ends the pipeline**. |

**Rule:** each op must accept the kind the previous op produced. Rankable
expressions are those whose final kind is `number` or `score`. For regex ops,
use `AsText()` first when values might not already be text. Do **not** insert
`AsText()` only to “prepare” a search op — extra ops before `Lexical` /
`Semantic` drop the warm source index and score the pipeline output instead.

| Op | Accepts | Produces |
| --- | --- | --- |
| `Value()` | `any` (first position only) | unchanged |
| `AsText()` | anything | `text` |
| `AsNumber()` | `any`, `text`, `number`, `text_or_list` | `number` |
| `RegexSearch(...)` | `any`, `text`, `text_or_list` | `text` |
| `RegexFindAll(...)` | `any`, `text`, `text_or_list` | `list_text` |
| `RegexSub(...)` | `any`, `text`, `list_text`, `text_or_list` | input kind (`any` → `text_or_list`) |
| `Slice(...)` | `any`, `text`, `list_text`, `text_or_list` | input kind (`any` → `text_or_list`) |
| `Length()` | `any`, `text`, `list_text`, `text_or_list` | `number` |
| `Lexical(...)` | `any`, `text`, `list_text`, `text_or_list` | `score` (terminal) |
| `Semantic(...)` | `any`, `text`, `list_text`, `text_or_list` | `score` (terminal) |

Regex uses a bounded RE2-style engine (not Python backtracking). No lookaround,
no backreferences. Pattern and replacement are capped at 16 KiB UTF-8.
Supported flags via `re`: `I`, `M`, `S` only (`re.A` / `re.U` are rejected —
word classes are ASCII).

---

### `Value`

```python
Value() -> Operation
```

Identity. First in the pipeline when reading the field as-is.

**Accepts:** `any` (first position only). **Produces:** unchanged.

---

### `AsText`

```python
AsText() -> Operation
```

Canonical text. `None` becomes `""`. That means a later `Semantic` scores the
empty string instead of `None`, and a later `Lexical` FTS-scores an empty
segment instead of the absent-cell `0.0` short-circuit.

**Accepts:** any pipeline kind. **Produces:** `text`.

---

### `AsNumber`

```python
AsNumber() -> Operation
```

Finite float from a number or numeric string. `None` stays `None`. `bool` and
non-numeric text raise `QuailRuntimeError` at evaluation.

**Accepts:** `any`, `text`, `number`, `text_or_list`. **Produces:** `number`.

---

### `RegexSearch`

```python
RegexSearch(pattern: str, flags: int = 0) -> Operation
```

First match substring (`group(0)`, not capturing groups), or `None` if there
is no match or the input cell is `None`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `pattern` | `str` | required | RE2-style pattern. |
| `flags` | `int` | `0` | `re.I`, `re.M`, `re.S` only; combine with `\|`. |

**Accepts:** `any`, `text`, `text_or_list` at construction. **Produces:** `text`.
At evaluation the cell must be `str` (or `None`); a list raises
`QuailRuntimeError` (`use AsText() first`).

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
| `flags` | `int` | `0` | `re.I`, `re.M`, `re.S` only. |

**Accepts:** `any`, `text`, `text_or_list` at construction. **Produces:** `list_text`.
At evaluation the cell must be `str` (or `None`); a list raises
`QuailRuntimeError` (`use AsText() first`).

---

### `RegexSub`

```python
RegexSub(pattern: str, replacement: str, flags: int = 0) -> Operation
```

Literal replace. The replacement is not a backreference template. `None`
input stays `None`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `pattern` | `str` | required | RE2-style pattern. |
| `replacement` | `str` | required | Literal replacement text. |
| `flags` | `int` | `0` | `re.I`, `re.M`, `re.S` only. |

**Accepts:** `any`, `text`, `list_text`, `text_or_list`.
**Produces:** the input kind (`any` → `text_or_list`).

---

### `Slice`

```python
Slice(start: int, end: int | None = None) -> Operation
```

Python slice `[start:end]` on text **or** list. `None` input stays `None`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `start` | `int` | required | Slice start (not `bool`). |
| `end` | `int` \| `None` | `None` | Slice end (not `bool`); `None` means through the last item. |

**Accepts:** `any`, `text`, `list_text`, `text_or_list`.
**Produces:** the input kind (`any` → `text_or_list`).

---

### `Length`

```python
Length() -> Operation
```

`len(text)`, `len(list)`, or `0` for `None`. Rankable (`number`). Other cell
types (including numbers) raise `QuailRuntimeError` at evaluation.

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
| `query` | `str` \| `list[str]` \| `GroupExpr` \| `list[Entry]` | required | Non-empty list of target texts (see shapes below). |
| `input_aggregation` | `"total"` \| `"avg"` \| `None` | `None` | How to combine scores on the entry side. `None` = `"total"`. |
| `target_aggregation` | `"total"` \| `"avg"` \| `None` | `None` | How to combine scores across targets. `None` = `"total"`. |

**Query shapes** — one rule: a **non-empty list of target texts**.

| Spell as | Meaning |
| --- | --- |
| `str` | One target. FTS query syntax (below). |
| `list[str]` | Each string is its own query. Target aggregation **sums** those scores (`None` / `"total"`) or takes their **mean** (`"avg"`). That is not the same as unquoted spaces inside one string (those are FTS OR). |
| Entry-scoped `GroupExpr` | Each member’s expression-root field becomes a target. |
| `list[Entry]` | Same, from explicit handles. |

**FTS syntax** (string queries)

| Syntax | Meaning |
| --- | --- |
| unquoted spaces | OR (not a phrase) |
| `"quoted text"` | Adjacent tokens |
| `term*` | Prefix of one clean term |
| uppercase `AND` / `NOT` | Operators. There is no `OR` keyword. |
| lowercase `and` / `not` / `or` | Ordinary terms |
| punctuation | Splits into terms the same way indexing does |

Entry-derived targets tokenize and quote their terms so prose like `AND` is
not an FTS operator.

**Scoring:** `score > 0` means “matched”. Unmatched and absent cells score
`0.0` (not `None`). Scores are corpus-relative.

**Accepts:** `any`, `text`, `list_text`, `text_or_list`. **Produces:** `score`.

**Errors:** `QuailSyntaxError` if the query is the wrong shape, an empty
`str` / `list[str]` / `list[Entry]`, or an aggregation is not `"total"` /
`"avg"` / `None`. `QuailScopeError` if a `GroupExpr` query is not
entry-scoped. `QuailRuntimeError` if resolved entry targets have no non-empty
text, or if search is not configured (repairable — fix the config and rerun
the whole exec).

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
usable in predicates and as a `retrieve` unit. Same query and aggregation
shapes as `Lexical`.

**Scoring:** cosine is **not** a match bit. Do not reuse Lexical’s
`score > 0` as “matched.” Empty cells score `None` (Lexical absence is `0.0`).

**Accepts:** `any`, `text`, `list_text`, `text_or_list`. **Produces:** `score`.

**Errors:** same construction-shape and configuration failures as `Lexical`
(empty `str` / `list[str]` / `list[Entry]`, wrong shape, bad aggregation,
non-entry group, unconfigured search, empty resolved entry targets). FTS parse
failures (whitespace-only or punctuation-only string queries) are **Lexical
only** — Semantic still embeds that string.

---

### Search performance

Both `Lexical` and `Semantic` run fastest on a **bare source field** (the
search op is the only op, field `kind` is `"source"`, and that field was
processed for search). Any prefix op — including identity `Value()` — skips
that warm index and scores a scratch corpus (Lexical scores are
corpus-relative, so they can differ). Transforming prefixes score the
**pipeline output**, not the raw cell. Analysis fields load and score cell
values.

`quail_export_csv` is the warm-path route to fast search over session tags —
see [Host tool: `quail_export_csv`](#host-tool-quail_export_csv).

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
| `unit` | `Unit` \| `Expression` | `entries` | What each list item is. An Expression yields one computed value per remaining entry. Not a `GroupExpr` or a string. |
| `group` | `GroupExpr` | `G0` | Population to draw from. Must match the unit’s scope. |
| `limit` | `int` | `1` | Positive int. Omitted `limit` is **1**, not the whole group. |
| `order` | `"top"` \| `"middle"` \| `"bottom"` | `"top"` | Slice of the ordered candidate sequence: `"top"` is the prefix `items[:limit]`; `"bottom"` is the suffix `items[-limit:]` (order preserved, not reversed); `"middle"` is a centered window `start = (len - limit) // 2`. |
| `rank` | `Ranking` \| `None` | `None` (`Ranking()`) | Ordering. Empty ranking = processing order. |

**Returns:** always a `list` (possibly empty). Item type depends on `unit`:

| `unit` | `group` scope | Items | Can rank? |
| --- | --- | --- | --- |
| `entries` / `Unit("entries")` | entries | `Entry` | yes |
| `fields` / `Unit("fields")` | fields | `Field` | no |
| `Unit("entries", field)` | entries | present values | yes |
| `Unit("values", field)` | entries | distinct values (full group, then `limit` / `order`) | no |
| `Expression` | entries | computed values | yes |

**Notes**

- Narrow with `.where` **before** expensive ranking when you can. Ranking
  scores the whole candidate set before applying `limit`.
- `fields` and `values` units cannot take a **non-empty** ranking
  (`QuailScopeError`). The default empty `Ranking()` is allowed.

**Errors:** `QuailSyntaxError` for bad argument shapes; `QuailScopeError` for
unit/group mismatch, ranking a non-rankable unit, or out-of-scope member
`Entry`s; `QuailFieldError` if a unit `Field` is unknown or kind-mismatched;
`QuailRuntimeError` if materializing an Expression fails (bad data, search
down, timeout).

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
| `group` | `GroupExpr` | `G0` | Population. Must match the unit’s scope. |

**Returns:** `int`. For `Unit("values", field)`, the number of distinct present
values over the full group. For `Unit("entries", field)`, the number of entries
where that field is present. For an `Expression` unit, `count` is `len` of the
group’s entry ids — the unit expression is **not** run (so a Lexical unit does
not require search to be configured, unlike `retrieve`).

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

**Returns:** `Field(name, kind="analysis")`. Creating an analysis field that
already exists is a no-op and returns that Field.

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

Write `value` onto `field` for every selected entry. Session overlay only.

| Name | Type | Description |
| --- | --- | --- |
| `group` | `GroupExpr` \| `list[Entry]` | Entry-scoped group, or a list of `Entry` handles. An empty list or empty group writes nothing; `field` is still resolved (unknown/source field still errors). |
| `field` | `Field` | Analysis field (create it first if needed). |
| `value` | `TagValue` | `bool`, `int`, finite `float`, `str`, lists, and dicts with string keys. **No `None` anywhere**, no cycles, no non-finite floats. |

**Returns:** `None`.

**Errors:** `QuailSyntaxError` if `field` is not a `Field`, `value` is `None` or
not a `TagValue`, or `group` is the wrong shape. `QuailScopeError` if `group`
is not entry-scoped, or a listed `Entry` belongs to another dataset or
version. `QuailFieldError` if `field` is source or unknown.

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
| `value` | `TagValue` \| `None` | `None` | `None` clears all selected present cells. A value clears cells where Python `==` matches (not JSON-canonical identity). |

**Returns:** `None`.

**Errors:** same shapes as `tag`. If `value` is not `None`, it must be a legal
tag payload.

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

**Returns:** `None`. The buffer is capped at 1 MiB UTF-8
(`QuailRuntimeError` if exceeded). Failed execs discard the buffer.

---

## Helpers

### `re`

```python
re.I: int
re.M: int
re.S: int

re.escape(pattern: str) -> str
```

Injected regex helper — **not** Python’s `re` module. Flags and `escape`
only. Pass flags into `RegexSearch` / `RegexFindAll` / `RegexSub`.

| Name | Meaning |
| --- | --- |
| `re.I` | Ignore case. Also `re.IGNORECASE`. |
| `re.M` | Multiline. Also `re.MULTILINE`. |
| `re.S` | Dot matches newline. Also `re.DOTALL`. |
| `re.escape(pattern)` | Escape a literal string for use in a pattern. `pattern` must be `str`. |

`re.A` / `re.U` exist as attributes but are **rejected** as regex flags
(RE2 word classes are ASCII).

---

## Errors

Exception classes exist for diagnostics. You **cannot catch** them inside
`quail_exec`. Read the diagnostic, fix the code, rerun the whole call.

```python
class QuailError(Exception): ...
class QuailSyntaxError(QuailError): ...
class QuailScopeError(QuailError): ...
class QuailFieldError(QuailError): ...
class QuailRuntimeError(QuailError): ...
```

| Class | Typical cause |
| --- | --- |
| `QuailSyntaxError` | Bad API shape, illegal Python construct, bad symbolic combo |
| `QuailScopeError` | Wrong group/unit/session/version pairing |
| `QuailFieldError` | Unknown field, kind mismatch, source mutation |
| `QuailRuntimeError` | Bad data for an op, search down, timeout, resource limit |

Failures are atomic: no tags, no bindings, no printed text.

A failed `quail_exec` returns a `diagnostic` object with `error_class`,
`stable_error_code`, `message`, and optional `repair_hint`. Prefer fixing from
that over guessing. (`execution_id` is `null` for these failures.)

---

## Bindings

After a **successful** exec, supported top-level names that still exist are
restored next time in the **same session**. Delete with `del name` if it
should not persist. Rebinding to a persistable value before the exec ends
is fine; only the **final** value of each name is encoded.

If that final value cannot persist, the whole exec fails (`QuailRuntimeError`)
— nothing commits. Typical messages: `Cannot persist value of type …`, or
`LiteralValue cannot contain …` (non-finite floats, cycles, non-string keys).

| Persist | Fail the exec |
| --- | --- |
| JSON-like scalars (including `None`), lists, and string-key dicts of JSON-like values — finite floats, no cycles | tuples, sets, callables, `re`, non-finite floats, cycles, non-string dict keys |
| Quail objects: `Field`, `Unit`, `Operation` instances (e.g. `Length()`), `Expression`, `Predicate`, `GroupExpr`, `Ranking`, `Entry` | Op **factories** (`Length` itself) and other callables |
| Lists of JSON-like values **and** lists of those Quail objects (including `retrieve` results; mixed lists of persistable objects are OK) | Dicts whose values are Quail objects (no list-style fallback) |

Analysis tags remain scoped to the session + dataset version. Bindings are
session-scoped.

---

## Python surface (bounded)

Allowed: literals, assignment (not `+=` / annotations / walrus), `if` / `for`
/ `while` on **concrete** values, calls to the injected API, `entry.value` /
`entry.fields` / `group.where` / `re.escape`, and these string methods:

`startswith`, `endswith`, `lower`, `upper`, `casefold`, `strip`, `lstrip`,
`rstrip`, `replace`, `split`, `rsplit`, `splitlines`, `count`, `find`,
`rfind`, `removeprefix`, `removesuffix`.

Not allowed (`QuailSyntaxError` at parse): imports, `def` / `lambda` /
`class`, comprehensions, f-strings, `try` / `except`, `raise` / `assert` /
`with`, `is`, `open` / `eval` / `exec`, mutating methods on containers,
item or attribute assignment/deletion (`xs[0] = …`, `obj.x = …` — rebind the
**name** instead), unlisted methods (including `str.join` / `str.format` and
`dict.get` / `.items` / `.keys` / `.values`), `re.compile`, anything that
reaches outside the sandbox.

---

## Host tool: `quail_export_csv`

`quail_export_csv` is a **host MCP tool**, not a name inside `quail_exec`.

```text
quail_export_csv(session_id, dataset_id)
```

Writes source columns plus this session’s **analysis fields** (created columns
and tags) to a CSV on the serve host. The tool result is not the file body:

```text
{"path": str, "session_id": str, "dataset_id": str,
 "dataset_version_id": str, "columns": list[str], "row_count": int}
```

`path` is a filesystem path on the serve host. The operator can process those
analysis columns as **source** columns later — the warm-path route to fast
`Lexical` / `Semantic` over session tags. Export itself does not reprocess.

Do not overlap it with `quail_exec` or another `quail_export_csv` on the same
`session_id` (`session_busy`).
