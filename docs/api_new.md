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

These types are **symbolic recipes**. Constructing them does not read data.
Evaluation happens only at `retrieve`, `count`, `tag`, `untag`, `entry.value`,
and `entry.fields`.

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
Query = str | list[str] | GroupExpr | list[Entry]
JSONLike = None | bool | int | float | str | list[JSONLike] | dict[str, JSONLike]
TagValue = JSONLike                           # None is forbidden anywhere in the tree
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
| `kind` | `"source"` \| `"analysis"` \| `None` | `None` | `None` resolves by name at use. An explicit kind must match the catalog when the Field is used or committed in a binding. |

**Attributes**

| Name | Type | Description |
| --- | --- | --- |
| `.name` | `str` | Column name. |
| `.kind` | `FieldKind` | Declared kind, or `None` to resolve later. |

**Errors**

| When | Error |
| --- | --- |
| `name` is missing or not a non-empty `str` | `QuailSyntaxError` |
| `kind` is not `"source"`, `"analysis"`, or `None` | `QuailSyntaxError` |
| Explicit `kind` does not match the catalog at use | `QuailFieldError` (the error names the registered kind — use it, or omit `kind`) |
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
| `field` | `Field` \| `None` | `None` | Required for `"values"`. Optional for `"entries"` (present values of that field, aligned to entries). Forbidden for `"fields"`. |

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
| `.dataset` | `str` | Dataset label on the handle. |

#### `Entry.value`

```python
entry.value(field: Field | str, default: Any = None) -> Any
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `field` | `Field` \| `str` | required | Column to read. A string must be non-empty. |
| `default` | `Any` | `None` | Returned when the cell is absent. |

**Returns:** the cell value, or `default` if absent. Empty cells are `None`, not `""`.

**Errors:** `QuailSyntaxError` if `field` is not a `Field` or non-empty `str`;
`QuailScopeError` if the handle is out of session/dataset scope;
`QuailRuntimeError` if called outside evaluation.

#### `Entry.fields`

```python
entry.fields() -> list[Field]
```

**Returns:** `Field` handles present on this row.

**Errors:** `QuailScopeError` if the handle is out of scope;
`QuailRuntimeError` if called outside evaluation.

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
| `*operations` | `Operation` | required (≥1) | Pipeline steps from the op factories (`Value()`, `Length()`, …). |

When `input` is a `Field`, the pipeline is exactly `*operations`. When `input`
is an `Expression`, `*operations` are appended. Start with `Value()` when
reading a field as-is.

**Returns:** a new `Expression`.

**Errors**

| When | Error |
| --- | --- |
| `input` is not a `Field` or `Expression` | `QuailSyntaxError` |
| No operations, or an argument is not an `Operation` | `QuailSyntaxError` |
| An op does not accept the previous op’s kind | `QuailSyntaxError` (names both sides) |
| `Value()` is not first, or `Lexical` / `Semantic` is not last | `QuailSyntaxError` |

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
| `==` `!=` | A sealed literal, `None`, or another `Expression`. Use `== None` / `!= None`, not `is None`. |
| `<` `<=` `>` `>=` | A finite numeric literal, or another `Expression`. |

**Ranking arithmetic** — an Expression used with `+` / `*` becomes a `Ranking`
(the expression must be rankable; see `Ranking`):

```python
(self + other: Expression | Ranking) -> Ranking
(self * weight: int | float) -> Ranking   # weight on the right, ≥ 0
```

**Forbidden**

- Python `and` / `or` / `not`, `if` / `while` on an Expression, or chained
  comparisons (`a < expr < b`).
- Iterating an Expression.

---

### `Predicate`

```python
class Predicate: ...
```

True/false recipe per entry. Produced by comparing an `Expression`. Do not
call `Predicate(...)` yourself.

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

Exactly one population form per constructor call:

| Form | Call | Meaning |
| --- | --- | --- |
| Named builtin | `GroupExpr("entries", name="G0")` / `GroupExpr("fields", name="G1")` | The injected `G0` / `G1`. |
| Filtered | `GroupExpr("entries", predicate=pred)` | Entries matching `pred`. |
| Members | `GroupExpr("entries", members=[...])` or `GroupExpr("fields", members=[...])` | Explicit `Entry` or `Field` handles matching `scope`. |

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
| `name` is not the matching builtin | `QuailSyntaxError` |

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
order. Non-empty = score each candidate, **higher first**.

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
| `*` | Scale by `weight`. `weight` must be a finite number `>= 0`, and must be on the **right** (`expr * 0.5`, not `0.5 * expr`). Cannot weight an empty Ranking. |

**Example**

```python
rank = score_a + score_b * 0.5
# or: Ranking(expression=score_a) + Ranking(expression=score_b) * 0.5
ranked = retrieve(group=matching, rank=rank, limit=10)
```

Use the **same** `group`, `rank`, `order`, and `limit` when pulling aligned
entries and scores.

**Errors**

| When | Error |
| --- | --- |
| `expression` is set but is not a rankable `Expression` | `QuailSyntaxError` |
| Weight is not a finite non-negative number, or is on the left | `QuailSyntaxError` |

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
expressions are those whose final kind is `number` or `score`. Use `AsText()`
first when values might not already be text.

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

Canonical text. `None` becomes `""`.

**Accepts:** any pipeline kind. **Produces:** `text`.

---

### `AsNumber`

```python
AsNumber() -> Operation
```

Finite float from a number or numeric string.

**Accepts:** `any`, `text`, `number`, `text_or_list`. **Produces:** `number`.

---

### `RegexSearch`

```python
RegexSearch(pattern: str, flags: int = 0) -> Operation
```

First match substring, or `None` if there is no match.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `pattern` | `str` | required | RE2-style pattern. |
| `flags` | `int` | `0` | `re.I`, `re.M`, `re.S` only; combine with `\|`. |

**Accepts:** `any`, `text`, `text_or_list`. **Produces:** `text`.

---

### `RegexFindAll`

```python
RegexFindAll(pattern: str, flags: int = 0) -> Operation
```

All matches as `list[str]`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `pattern` | `str` | required | RE2-style pattern. |
| `flags` | `int` | `0` | `re.I`, `re.M`, `re.S` only. |

**Accepts:** `any`, `text`, `text_or_list`. **Produces:** `list_text`.

---

### `RegexSub`

```python
RegexSub(pattern: str, replacement: str, flags: int = 0) -> Operation
```

Literal replace. The replacement is not a backreference template.

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

Python slice `[start:end]` on text **or** list.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `start` | `int` | required | Slice start (not `bool`). |
| `end` | `int` \| `None` | `None` | Slice end; `None` means through the last item. |

**Accepts:** `any`, `text`, `list_text`, `text_or_list`.
**Produces:** the input kind (`any` → `text_or_list`).

---

### `Length`

```python
Length() -> Operation
```

`len(text)`, `len(list)`, or `0` for `None`. Rankable (`number`).

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
| `list[str]` | Each string is its own query; the list is OR’d. |
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

Entry-derived targets tokenize and quote their terms (OR).

**Scoring:** `score > 0` means “matched”. Scores are corpus-relative.

**Accepts:** `any`, `text`, `list_text`, `text_or_list`. **Produces:** `score`.

**Errors:** `QuailSyntaxError` if the query is empty or the wrong shape, or an
aggregation is not `"total"` / `"avg"` / `None`. `QuailScopeError` if a
`GroupExpr` query is not entry-scoped. If search is not configured, the
diagnostic is repairable — fix the config and rerun the whole exec.

---

### `Semantic`

```python
Semantic(
    query: Query,
    input_aggregation: Aggregation = None,
    target_aggregation: Aggregation = None,
) -> Operation
```

Embedding cosine similarity under the dataset embedding profile (configured
outside this API). Terminal. Rankable (`score`). Same query and aggregation
shapes as `Lexical`.

**Scoring:** cosine is **not** a match bit. Do not reuse Lexical’s
`score > 0` as “matched.” Empty cells score `None`.

**Accepts:** `any`, `text`, `list_text`, `text_or_list`. **Produces:** `score`.

**Errors:** same query / aggregation / configuration failures as `Lexical`.

---

### Search performance

Both `Lexical` and `Semantic` run fastest on a **bare source field** (no ops
before the search op) that was processed for search. Transformed values and
analysis fields load and score cell values instead.

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
| `order` | `"top"` \| `"middle"` \| `"bottom"` | `"top"` | Which slice of the ordered candidate sequence to take. |
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
- `fields` and `values` units cannot be ranked (`QuailScopeError`).

**Errors:** `QuailSyntaxError` for bad argument shapes; `QuailScopeError` for
unit/group mismatch or ranking a non-rankable unit.

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
where that field is present. For an `Expression` unit, the number of matching
entries (the expression is not evaluated just to count).

---

### `create_field`

```python
create_field(field: str | Field) -> Field
```

Create a session-only analysis column. Source fields cannot be created or
overwritten.

| Name | Type | Description |
| --- | --- | --- |
| `field` | `str` \| `Field` | Column name, `Field("topic")`, or `Field("topic", "analysis")`. |

**Returns:** `Field(name, kind="analysis")`. Creating an analysis field that
already exists is a no-op and returns that Field.

**Errors:** `QuailSyntaxError` if the name is empty, the argument is the wrong
type, or `Field.kind` is `"source"`. `QuailFieldError` if the name collides
with a source field.

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
| `group` | `GroupExpr` \| `list[Entry]` | Entry-scoped group, or a list of `Entry` handles. An empty list is a no-op. |
| `field` | `Field` | Analysis field (create it first if needed). |
| `value` | JSON-like | `bool`, `int`, finite `float`, `str`, lists, and dicts with string keys. **No `None` anywhere**, no cycles, no non-finite floats. |

**Returns:** `None`.

**Errors:** `QuailSyntaxError` if `field` is not a `Field`, `value` is `None` or
not JSON-like, or `group` is the wrong shape. `QuailScopeError` if `group` is
not entry-scoped. `QuailFieldError` if `field` is source or unknown.

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
| `group` | `GroupExpr` \| `list[Entry]` | required | Same as `tag`. Empty selection is a no-op. |
| `field` | `Field` | required | Analysis field to clear. |
| `value` | `TagValue` \| `None` | `None` | `None` clears all selected cells. A value clears **exact matches** only. |

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

Tool errors include `stable_error_code`, `message`, optional `repair_hint`, and
optional source location. Prefer fixing from that over guessing.

---

## Bindings

After a **successful** exec, supported top-level names you assigned are
restored next time in the **same session**. Delete with `del name` if it
should not persist.

| Persist | Do not persist |
| --- | --- |
| JSON-like values | tuples, sets, callables, and similar |
| Quail symbolic objects (`Field`, `Expression`, `Predicate`, `GroupExpr`, `Ranking`, `Unit`, `Entry`, ops) | Anything that cannot round-trip as a binding |

Analysis tags remain scoped to the session + dataset version. Bindings are
session-scoped.

---

## Python surface (bounded)

**Allowed in spirit:** literals, assignment, `if` / `for` / `while` on
**concrete** values, calls to the injected API, `entry.value` / `entry.fields`
/ `group.where` / `re.escape`, and these string methods:

`startswith`, `endswith`, `lower`, `upper`, `casefold`, `strip`, `lstrip`,
`rstrip`, `replace`, `split`, `rsplit`, `splitlines`, `count`, `find`,
`rfind`, `removeprefix`, `removesuffix`.

**Not allowed:** imports, `def` / `lambda`, comprehensions, f-strings,
`try` / `except`, `is`, `open` / `eval` / `exec`, mutating methods on
containers (rebind instead), anything that reaches outside the sandbox.

---

## Host tool: `quail_export_csv`

`quail_export_csv` is a **host MCP tool**, not a name inside `quail_exec`.

```text
quail_export_csv(session_id, dataset_id)
```

Writes source columns plus this session’s tags to a CSV **path** on the serve
host (a filesystem path, not the file body) so the operator can process those
tags as **source** columns later — the warm-path route to fast `Lexical` /
`Semantic` over session tags. Export itself does not reprocess.

Do not overlap it with `quail_exec` on the same `session_id` (`session_busy`).
