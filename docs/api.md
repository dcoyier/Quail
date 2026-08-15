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
| `session_id` | Durable analysis context in one workspace. Bindings and tags stick to this session. MCP binds the workspace; if the workspace changes, start a new session. Run one `quail_exec` at a time per `session_id` — overlap (including `quail_export_csv` on the same session) fails with `session_busy`. A full process-wide exec slot fails with `server_busy` (raise `hosting.max_concurrent_executions` and restart `quail run`). |
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

## Start here

The shipped `examples/data/notes.csv` columns are `id`, `title`, `body`. Inspect
`G1` on other datasets. Empty cells are `None`, not `""`.

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
body = Field("body")
mentions = Expression(body, RegexSearch("hydrangea", flags=re.I)) != None
matching = G0.where(mentions)

print("matches", count(group=matching))
for entry in retrieve(group=matching, limit=10):
    text = entry.value(body) or ""
    print(entry.id, text[:500])
```

### 4. Rank with lexical search

```python
score = Expression(Field("body"), Lexical('"hydrangea care"'))
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
    Expression(Field("body"), RegexSearch("climate", flags=re.I)) != None
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

Deployments bound each `quail_exec` with fixed product ceilings (not TOML knobs):

- `time_window="standard"`: 30s wall-clock, 15s worker CPU
- `time_window="extended"`: 100s wall-clock, 60s worker CPU
- Worker RSS memory: 256 MiB in both windows

Extended only lengthens time. Hitting any ceiling fails the whole exec
atomically (no tags, bindings, or printed output).

---

## Types (short)

### `Field(name, kind=None)`

`kind` is `"source"`, `"analysis"`, or `None` (resolve by name at use).  
Explicit kind must match the catalog when the Field is used, and when a
binding that holds the Field (or a tree containing it) is committed.
Restore does not fail the exec on a stale kind (bindings are session-global;
kinds are dataset-specific) — using the Field still raises, and `del` recovers.
Construction alone does not consult the catalog.
Do not compare a Field to a value or order Fields — that raises
`QuailSyntaxError`; use `Expression(field, Value())` (or a numeric op) for
entry-value predicates. `Field == Field` is only identity of `(name, kind)`,
not a value predicate.

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
| `Slice(start, end=None)` | Python slice `[start:end]` on text **or** list |
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
flags via `re`: `I`, `M`, `S` only. `re.A` and `re.U` exist on the helper but
are rejected — RE2 word classes are ASCII. No lookaround, no backreferences.

### Predicates and groups

```python
pred = Expression(Field("body"), Length()) >= 500
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

Do **not** wrap an already-combined Ranking in `Ranking(expression=…)` — that
constructor takes a single Expression, not a Ranking.

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
- `unit` may be a `Unit` or an `Expression` (expression → one value per entry).
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

- **Lexical:** `score > 0` means “matched”. String queries use Turso FTS
  syntax: unquoted spaces are OR (not a phrase); uppercase `AND` / `NOT` are
  operators (lowercase `and` / `not` / `or` are ordinary terms); `"quoted
  text"` is adjacent tokens; `term*` prefixes one clean term. There is no
  `OR` keyword. Hyphens and other punctuation split the same way indexing
  does (an unquoted atom matches any resulting term). `list[str]` parses each
  string as its own query and ORs them. Entry-derived targets
  (`GroupExpr` / `list[Entry]`) read the expression root field, tokenize it,
  and quote those terms (OR). Cell prose is not query syntax — `AND` in the
  field is a word. Scores are corpus-relative.
  On a bare source field (no `Slice` / `AsText` / … in front), scoring uses the
  process-warmed FTS index and does not load source cells.
- **Semantic:** exact cosine similarity under the dataset embedding profile
  (configured outside this API; scored in Turso, not approximate ANN). Cosine
  is not a match bit — do not copy Lexical’s `score > 0` as “matched.” Empty
  cells score `None`. The same four query shapes work; entry targets read the
  expression root field. If search
  isn’t available, you get a repairable runtime diagnostic — fix config and
  rerun the whole exec.
  On a bare source field (no `Slice` / `AsText` / … in front) that was included
  in the dataset embedding field set at `quail process`, scoring uses the
  process-warmed segment map and does not load source cells. Prefix ops,
  analysis fields, and source fields omitted from that set still materialize
  cells. Re-run `quail process` after this layout change.
  Re-processing the **same dataset id** (same `[[datasets]] id`) reuses
  embeddings for unchanged text when the embedding profile is unchanged.
  `--clear` and a new id rebuild. Analysis tags stay session-only and do not
  use the warmed path.

`quail_export_csv` is a **host MCP tool**, not a name inside `quail_exec`. Call
it with `session_id` and `dataset_id`. It writes source columns plus this
session's tags to a CSV on the serve host (a filesystem `path`, not the
file body). That is the route to warm-path speed for session tags: stop
`quail run`, point the same dataset `id` `source` at that path in the TOML
(the CLI never writes it), then `quail process` so those columns are **source**
and `Lexical` / `Semantic` skip cell load. Export itself does not reprocess.
Do not overlap it with `quail_exec` on the same `session_id` (`session_busy`).

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
| `QuailRuntimeError` | Bad data for an op, search down, timeout, resource limit. Overlap on one `session_id` is `session_busy`. A full process exec slot is `server_busy`. |

Tool errors include `stable_error_code`, `message`, optional `repair_hint`, and
optional source location. Prefer fixing from that over guessing.

---

## Notes for maintainers

This file is what agents read for `quail_exec`. Prefer workflows and short
contracts over encyclopedic edge lists. Expand a section when agents repeatedly
miss it; don’t restore the old long form by default.

Open knobs:

- How much query-syntax / aggregation detail agents need up front
- Exact diagnostic field schema (keep stable codes; trim redacted_context lore)
