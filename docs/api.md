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

Empty cells are `None`, not `""`. Imported CSV cells are stripped strings, not
numbers — use `AsNumber()` for numeric compare.

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
| Safe builtins | `abs`, `all`, `any`, `bool`, `dict`, `enumerate`, `float`, `int`, `len`, `list`, `max`, `min`, `range`, `repr`, `round`, `set`, `str`, `sum`, `tuple`, `zip` |

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

## Types (short)

### `Field(name, kind=None)`

`kind` is `"source"`, `"analysis"`, or `None` (resolve by name at use).
An explicit kind must match the catalog when the Field is used or committed
in a binding; the error tells you the registered kind — use it or omit kind.
A restored binding with a stale kind does not fail the exec; using the Field
still raises, and `del name` recovers.
Fields are names, not values: do not compare a Field to a value or order
Fields — use `Expression(field, Value())` (or a numeric op) for entry-value
predicates.

### `Unit(scope, field=None)`

- `Unit("entries")` — entries (also `entries`)
- `Unit("entries", field)` — present values of `field`, aligned to entries
- `Unit("fields")` — fields (also `fields`; use with a field group like `G1`)
- `Unit("values", field)` — distinct present values over the full group;
  `limit` / `order` apply to that distinct sequence (not to entries first)

### `Entry` (no public constructor)

From `retrieve`. Attributes: `.id`, `.dataset_id`, `.dataset_version_id`, `.dataset`.

```python
entry.value(field, default=None)  # Field or name string
entry.fields()                    # list[Field] present on this entry
```

### `Expression(input, operation, ...)`

`input` is a `Field` or another `Expression`. An empty pipeline is identity
(the field value). `Value()` is the same identity and is dropped when other
ops follow.

Comparisons (`==`, `!=`, `<`, …) produce a **Predicate**.

### Operations

| Op | Role |
| --- | --- |
| `Value()` | Identity; first in pipeline when reading the field |
| `AsText()` | Canonical text (`None` → `""`) |
| `AsNumber()` | Finite float from number or numeric string |
| `RegexSearch(pattern, flags=0)` | First match substring, or `None` |
| `RegexFindAll(pattern, flags=0)` | `list[str]` of matches |
| `RegexSub(pattern, replacement, flags=0)` | Literal replace (no backrefs) |
| `Slice(start, end=None)` | Python slice `[start:end]` on text **or** list |
| `Length()` | len(text), len(list), or `0` for `None` |
| `Lexical(query, ...)` | Lexical relevance score (ends the pipeline) |
| `Semantic(query, ...)` | Embedding similarity score (ends the pipeline) |

Pipelines are type-checked at construction: each op must accept what the
previous op produces, and the error names both sides. Use `AsText()` first
when values might not already be text. `Lexical` / `Semantic` end the
pipeline; rankable expressions end in `AsNumber`, `Length`, `Lexical`, or
`Semantic`. `Lexical` / `Semantic` are ordinary score expressions — use them
in predicates or as a `retrieve` unit; pass them to `rank=` or wrap them in
`Ranking(expression=...)` for ordered retrieval.

Regex uses a bounded RE2-style engine (not Python backtracking). Supported
flags via `re`: `I`, `M`, `S` only (`re.A` / `re.U` are rejected — word
classes are ASCII). No lookaround, no backreferences.

### Predicates and groups

```python
pred = Expression(Field("body"), Length()) >= 500
mentions = Expression(Field("body"), RegexSearch("hydrangea")) != None
group = G0.where(pred)
both = pred_a & pred_b
either = pred_a | pred_b
not_pred = ~pred
```

`GroupExpr("entries", predicate=...)` or `members=[...]` (entries or fields,
matching scope). Compose groups with `&` `|` `~`. Materialize with `retrieve` /
`count` — do not iterate a GroupExpr directly.

### `Ranking(expression=None)`

Empty ranking = processing order. Non-empty = score each candidate, higher
first. Combine rankable expressions (or `Ranking` values) with `+` and weight
with `expr * weight` (weight on the right, non-negative). Example:

```python
rank = score_a + score_b * 0.5
# or: Ranking(expression=score_a) + Ranking(expression=score_b) * 0.5
ranked = retrieve(group=matching, rank=rank, limit=10)
```

`Ranking(expression=…)` takes a single Expression — combined Rankings are
already Rankings and need no wrapping.

Use the **same** group, rank, order, and limit when pulling aligned entries and
scores.

---

## `retrieve` and `count`

```python
retrieve(unit=entries, group=G0, limit=1, order="top", rank=Ranking())
count(unit=entries, group=G0)
```

- `retrieve` always returns a **list** (possibly empty).
- Omitted `limit` defaults to **1** (not the whole group).
- `retrieve` `unit` may be a `Unit` or an `Expression` (expression → one value per entry).
- `count` `unit` is a `Unit` only — filter with `.where`, then count the group.
- `group` may be a `GroupExpr` or a `list[Entry]`.
- `rank` may be a `Ranking` or a rankable `Expression`.
- `order`: `"top"` | `"middle"` | `"bottom"`.
- Narrow with `.where` **before** expensive ranking when you can — ranking
  scores the whole candidate set before applying `limit`.

| Unit | Group | Items | Can rank? |
| --- | --- | --- | --- |
| entries | entry group | `Entry` | yes |
| fields | field group | `Field` | no |
| `Unit("entries", field)` | entry group | present values | yes |
| `Unit("values", field)` | entry group | distinct values (full group, then limit/order) | no |
| `Expression` | entry group | computed values | yes |

---

## Mutations

```python
create_field("topic")           # or Field("topic") / Field("topic", "analysis")
tag(group_or_entries, field, value)      # value: JSON-like, no None inside
untag(group_or_entries, field)           # clear all selected
untag(group_or_entries, field, value)    # clear JSON-text identity matches
```

Source fields cannot be created or overwritten. The name `id` is illegal on
`create_field` (`entry.id` is the row id). Empty selections are no-ops.

---

## Lexical and semantic search

```python
Lexical(query, input_aggregation=None, target_aggregation=None)
Semantic(query, input_aggregation=None, target_aggregation=None)
```

A query is a **non-empty list of target texts**, spelled as a `str`,
`list[str]`, an entry-scoped `GroupExpr`, or `list[Entry]` (entry shapes read
each entry’s expression root field). Aggregations: `"total"`, `"avg"`, or
`None` (= total).

- **Lexical:** FTS relevance; `score > 0` means “matched”, and scores are
  corpus-relative. String queries use FTS syntax: unquoted spaces are OR (not
  a phrase); `"quoted text"` is adjacent tokens; `term*` prefixes one clean
  term; uppercase `AND` / `NOT` are operators and there is no `OR` keyword
  (lowercase `and` / `not` / `or` are ordinary terms). Punctuation splits into
  terms the same way indexing does. `list[str]` ORs each string as its own
  query; entry-derived targets tokenize and quote their terms (OR).
- **Semantic:** exact cosine similarity under the dataset embedding profile
  (configured outside this API). Cosine is not a match bit — do not reuse
  Lexical’s `score > 0` as “matched.” Empty cells score `None`.

Both run fastest on a bare source field (no ops before the search op) that was
processed for search; transformed values and analysis fields load and score
cell values instead. If search is not configured, the diagnostic is
repairable — fix the config and rerun the whole exec.

`quail_export_csv` is a **host MCP tool**, not a name inside `quail_exec`.
Called with `session_id` and `dataset_id`, it writes source columns plus this
session’s tags to a CSV path on the serve host (a filesystem path, not the
file body) so the operator can process those tags as **source** columns later
— the warm-path route to fast `Lexical` / `Semantic` over session tags.
Export itself does not reprocess. Do not overlap it with `quail_exec` on the
same `session_id` (`session_busy`).

---

## Bindings and print

After a **successful** exec, supported top-level names you assigned are
restored next time in the **same session**. Delete with `del name`
if it should not persist. Prefer JSON-like values and Quail symbolic objects;
tuples/sets/callables and similar cannot persist. Analysis tags remain scoped
to the session + dataset version; bindings are session-scoped.

```python
print(*values, sep=" ", end="\n")
```

That buffer is the only caller-visible analysis output.

---

## Python surface (bounded)

Allowed in spirit: literals, assignment, `if`/`for`/`while` on **concrete**
values, calls to the injected API, listed string methods (`lower`, `split`, …),
`entry.value` / `entry.fields` / `group.where` / `re.escape`.

Not allowed: imports, `def`/`lambda`, comprehensions, f-strings, `try`/`except`,
`is`, `open`/`eval`/`exec`, mutating methods on containers (rebind instead),
anything that reaches outside the sandbox.

Exception classes exist for diagnostics; you cannot catch them inside
`quail_exec`. Read the diagnostic, fix the code, rerun the whole call.

---

## When things fail

Failures are atomic. Typical categories:

| Class | Typical cause |
| --- | --- |
| `QuailSyntaxError` | Bad API shape, illegal Python construct, bad symbolic combo |
| `QuailScopeError` | Wrong group/unit/session/version pairing |
| `QuailFieldError` | Unknown field, kind mismatch, source mutation |
| `QuailRuntimeError` | Bad data for an op, search down, timeout, resource limit |

Tool errors include `stable_error_code`, `message`, optional `repair_hint`, and
optional source location. Prefer fixing from that over guessing.
