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
| `session_id` | Durable analysis context. Bindings and tags stick to this session. |
| `dataset_id` | Exactly one dataset for this call (its active immutable version). |
| `code` | Bounded Quail Python (no imports, no files, no network). |
| `time_window` | `"standard"` or `"extended"` — both are finite; extended is just longer. |

**Success:** `{"printed_output": "<exactly what print() wrote>"}`.  
**Failure:** a tool error with `execution_id` (or `null`) and a `diagnostic`.
Nothing partial is kept if quail_exec fails — no tags, no bindings, no printed text.

Only `print(...)` leaves the sandbox. Return values of expressions do not.

---

## Vocabulary

| Term | Meaning |
| --- | --- |
| **Entry** | One row in the dataset. |
| **Field** | One column: source (imported, immutable) or analysis (session tags). |
| **Expression** | Recipe that reads/transforms one field’s value per entry. |
| **Predicate** | True/false recipe per entry (usually from comparing expressions). |
| **Group** | Symbolic set of entries or fields — not a Python list until you retrieve. |
| **Unit** | What `retrieve`/`count` should return (entries, fields, values, …). |
| **Ranking** | How to score and order entries. |
| **Binding** | Top-level name that survives a successful exec in this session+dataset. |
| **Mutation** | `create_field` / `tag` / `untag` — session overlay only. |

**Symbolic vs materialized:** building `Expression(...)` or `G0.where(...)`
does not read the data. Quail evaluates when you `retrieve`, `count`,
`entry.value`, `tag`, etc.

---

## Start here

### 1. Look at fields

```python
for field in retrieve(unit=fields, group=G1, limit=50):
    print(field.name, field.kind)

samples = retrieve(limit=1)
if len(samples) > 0:
    sample = samples[0]
    for field in sample.fields():
        print(field.name, repr(sample.value(field)))
```

### 2. Pull a few entries

```python
for entry in retrieve(group=G0, limit=10):
    print(entry.id)
```

### 3. Filter with regex

```python
content = Field("content")
mentions = Expression(content, RegexSearch("hydrangea", flags=re.I)) != None
matching = G0.where(mentions)

print("matches", count(group=matching))
for entry in retrieve(group=matching, limit=10):
    print(entry.id, entry.value(content)[:500])
```

### 4. Rank with lexical search

```python
score = Expression(Field("content"), Lexical("hydrangea care"))
matching = G0.where(score > 0)
rank = Ranking(expression=score)

ranked_entries = retrieve(group=matching, rank=rank, limit=10)
ranked_scores = retrieve(unit=score, group=matching, rank=rank, limit=10)

for i in range(len(ranked_entries)):
    print(ranked_entries[i].id, ranked_scores[i])
```

### 5. Tag analysis labels (session only)

```python
selected = G0.where(
    Expression(Field("content"), RegexSearch("climate", flags=re.I)) != None
)
topic = create_field("topic")
tag(selected, topic, "climate")
print(count(group=G0.where(Expression(topic, Value()) == "climate")))
```

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

Deployments also bound time, memory, output size, call counts, and similar.
Hitting a limit fails the whole exec.

---

## Types (short)

### `Field(name, kind=None)`

`kind` is `"source"`, `"analysis"`, or `None` (resolve by name).  
Do not compare fields directly — use `Expression(field, Value())`.

### `Unit(scope, field=None)`

- `Unit("entries")` — entries (also `entries`)
- `Unit("entries", field)` — present values of `field`, aligned to entries
- `Unit("fields")` — fields (also `fields`; use with a field group like `G1`)
- `Unit("values", field)` — distinct present values

### `Entry` (no public constructor)

From `retrieve`. Attributes: `.id`, `.dataset_id`, `.dataset_version_id`, `.dataset`.

```python
entry.value(field, default=None)  # Field or name string
entry.fields()                    # list[Field] present on this entry
```

### `Expression(input, operation, ...)`

`input` is a `Field` or another `Expression`. Pipeline must start with `Value()`
when reading a field as-is; nest to append ops.

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
| `Slice(start, end=None)` | Text slice `[start:end]` |
| `Length()` | len(text), len(list), or `0` for `None` |
| `Lexical(query, ...)` | Lexical relevance score (ends the pipeline) |
| `Semantic(query, ...)` | Embedding similarity score (ends the pipeline) |

Use `AsText()` before regex when values might not already be text.  
`Lexical` / `Semantic` must be the **last** op. Rankable scores end in
`AsNumber`, `Length`, `Lexical`, or `Semantic`.
`Lexical` / `Semantic` are ordinary score expressions — they are **not** tied to
`Ranking`. Use them in predicates or as a `retrieve` unit with no ranking; wrap
them in `Ranking(expression=...)` only when you want ordered retrieval.

Regex uses a bounded RE2-style engine (not Python backtracking). Supported
flags via `re`: `I`, `M`, `S`, `A`/`U`, etc. No lookaround, no backreferences.

### Predicates and groups

```python
pred = Expression(Field("content"), Length()) >= 500
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
first. Combine with `+` and weight with `expr * weight` (weight on the right,
non-negative).

Use the **same** group, rank, order, and limit when pulling aligned entries and
scores.

---

## `retrieve` and `count`

```python
retrieve(unit=entries, group=G0, limit=1, order="top", rank=Ranking())
count(unit=entries, group=G0)
```

- `retrieve` always returns a **list** (possibly empty).
- `unit` may be a `Unit` or an `Expression` (expression → one value per entry).
- `order`: `"top"` | `"middle"` | `"bottom"`.
- Narrow with `.where` **before** expensive ranking when you can — ranking
  scores the whole candidate set before applying `limit`.

| Unit | Group | Items | Can rank? |
| --- | --- | --- | --- |
| entries | entry group | `Entry` | yes |
| fields | field group | `Field` | no |
| `Unit("entries", field)` | entry group | present values | yes |
| `Unit("values", field)` | entry group | distinct values | no |
| `Expression` | entry group | computed values | yes |

---

## Mutations

```python
create_field("topic")           # or Field("topic") / Field("topic", "analysis")
tag(group_or_entries, field, value)      # value: JSON-like, no None inside
untag(group_or_entries, field)           # clear all selected
untag(group_or_entries, field, value)    # clear exact matches only
```

Source fields cannot be created or overwritten. Empty selections are no-ops.

---

## Lexical and semantic search

```python
Lexical(query, input_aggregation=None, target_aggregation=None)
Semantic(query, input_aggregation=None, target_aggregation=None)
```

`query`: non-empty `str`, `list[str]`, entry `GroupExpr`, or `list[Entry]`.  
Aggregations: `"total"`, `"avg"`, or `None` (= total).

- **Lexical:** `score > 0` means “matched”; string queries support simple
  query syntax (phrases, `AND` / `NOT`, `term*`). Scores are corpus-relative.
- **Semantic:** cosine similarity under the workspace embedding profile
  (configured outside this API). If search isn’t available, you get a
  repairable runtime diagnostic — fix config and rerun the whole exec.

---

## Bindings and print

After a **successful** exec, supported top-level names you assigned are
restored next time in the same session + dataset version. Delete with `del name`
if it should not persist. Prefer JSON-like values and Quail symbolic objects;
tuples/sets/callables and similar cannot persist.

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

---

## Notes for maintainers

This file is what agents read for `quail_exec`. Prefer workflows and short
contracts over encyclopedic edge lists. Expand a section when agents repeatedly
miss it; don’t restore the old long form by default.

Open knobs:

- How much query-syntax / aggregation detail agents need up front
- How loud to be about resource limits vs keeping that deployment-local
- Exact diagnostic field schema (keep stable codes; trim redacted_context lore)
