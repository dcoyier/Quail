# Quail Analysis API

> **Unpublished.** Next analysis contract. `quail_get_api_docs` still serves
> [`api.md`](api.md) until the runtime matches this file.

Quail is a Python cell over one private dataset. You call `quail_exec` and
write Python against an injected analysis library. **Only `print` returns.**
The dataset is an immutable grid. The session overlay holds analysis fields
and tags.

You compose filters, scores, groups, and rankings as recipes — construction
does not read cells — then evaluate them with `retrieve`, `count`, `tag`,
`untag`, `get`, and `entry[...]`. Any corpus operation is a composition of
those pieces, or a Python function attached with `.map`. The cell is ordinary
Python. The process has no network and no filesystem.

---

## `quail_exec`

Host MCP tool, not a name inside the cell. Pass arguments by name.

```text
quail_exec(session_id, dataset_id, code, time_window="standard")
```

| Argument | Meaning |
| --- | --- |
| `session_id` | Analysis context in one workspace. Namespace, analysis fields, and tags persist across successful cells. After `quail_switch_workspace`, start a new session. One in-flight `quail_exec` or `quail_export_csv` per session (`session_busy`); process-wide capacity exhausted (`server_busy`). Retry the same session. |
| `dataset_id` | One dataset; its active immutable version. `entry.dataset_version_id` is which version you got. |
| `code` | One Python cell. |
| `time_window` | `"standard"` (30s wall / 15s CPU) or `"extended"` (100s wall / 60s CPU). Omitted = `"standard"`. Resident memory 256 MiB either way. |

**Success:** `{"printed_output": "<print buffer>"}`.

**Failure:**

```text
{"execution_id": null, "diagnostic": {
    "error_class": str,
    "stable_error_code": str,
    "message": str,
    "repair_hint": str   # omitted when absent
}}
```

If the cell finishes without an uncaught exception, this cell's overlay writes
and namespace commit — including writes before a handled exception. An
uncaught exception or a resource limit restores overlay, namespace, and prints
to the last successful cell. MCP argument-validation failures may use the
client's native tool-error form.

---

## A session

Field names differ per dataset. Inspect, then compose.

```python
print(count(G1), "fields;", count(G0), "entries")
for field in retrieve(G1, 50):
    print(field.name, field.kind)

sample = retrieve()
if sample:
    row = sample[0]
    print("id", row.id)
    for field in row.fields():
        print(field.name, repr(row[field]))

energy = G0.where(F.body.search(r"(?i)\benergy\b") != None)
climate = G0.where(F.body.lexical("climate environment emissions") > 0)
both = energy & climate
print("energy", count(energy), "both", count(both))

score = F.body.lexical("clean energy") + F.body.semantic("climate policy") * 0.5
for row in retrieve(both, 5, rank=score):
    print(row.id, row["body"])

theme = create_field("theme")
tag(both, theme, "energy-climate")
for y in retrieve(both, 20, of=F.year, distinct=True):
    print(y, count(both.where(F.year == y)))
```

Imported blank cells are `None`. A stored `""` is an empty string. Imported
CSV cells are stripped strings — `F.year.number()` for numeric compare. The
CSV `id` column is `row.id` / `get(id)`.

---

## The cell

`code` is one Python module body: `def`, `class`, `import`, comprehensions,
f-strings, `try`, mutation. Analysis names are already bound. `import` of the
standard library works; anything that needs the network or filesystem does not.

A successful cell's namespace is the next cell's starting namespace: recipes,
JSON-like data, functions, classes, lists of those, imported modules. If a
top-level name cannot persist, the cell fails. `del name` drops a name.
`.map(fn)` persists when `fn` does — write a `def`.

Only `print(...)` is copied to `printed_output`.

```python
print(*values, sep=" ", end="\n")
```

---

## Namespace

| Kind | Names |
| --- | --- |
| Callables | `retrieve`, `count`, `create_field`, `tag`, `untag`, `get`, `print` |
| Groups | `G0`, `G1` |
| Field sugar | `F` — `F.body` is `Field("body")` |
| Units | `entries`, `fields` |
| Types | `Field`, `Unit`, `Expression`, `Predicate`, `GroupExpr`, `Ranking`, `Entry`, `Operation` |
| Ops | `Value`, `AsText`, `AsNumber`, `RegexSearch`, `RegexFindAll`, `RegexSub`, `Slice`, `Length`, `Lexical`, `Semantic`, `Map` |
| Errors | `QuailError`, `QuailSyntaxError`, `QuailScopeError`, `QuailFieldError`, `QuailRuntimeError` |

- **`G0`**: all entries, import order. **`G1`**: all fields, source then analysis.
- **`F`**: `F.body` and `F["class year"]` are `Field` handles (identity recipes). Methods return `Expression`.
- Pipeline regex flags: `import re`, then `re.I` / `re.M` / `re.S`. Inline `(?i)` needs no import.

---

## `Field` and `Expression`

```python
Field(name: str, kind: "source" | "analysis" | None = None) -> Field
F.body                             # Field("body")
F["class year"]                    # Field("class year")
```

A `Field` is a column handle and the identity recipe for that column. `.name`
and `.kind` (`"source"`, `"analysis"`, or `None` until resolved) are catalog
facts. Compare two catalog handles by `.name`. Compare cells with
`F.body == value` (a `Predicate`). `kind=` must match the catalog when the
field is used; omit it unless you mean to check.

```python
Query = str | list[str] | GroupExpr | list[Entry]   # GroupExpr is entry-scoped
Aggregation = "total" | "avg" | None     # None = "total"
```

```python
class Field:
    name: str
    kind: str | None

    def text(self) -> Expression: ...
    def number(self) -> Expression: ...
    def length(self) -> Expression: ...
    def search(self, pattern: str, flags: int = 0) -> Expression: ...
    def findall(self, pattern: str, flags: int = 0) -> Expression: ...
    def sub(self, pattern: str, replacement: str, flags: int = 0) -> Expression: ...
    def slice(self, start: int, end: int | None = None) -> Expression: ...
    def __getitem__(self, item: slice) -> Expression: ...   # [start:end] → .slice
    def lexical(self, query: Query, input_aggregation: Aggregation = None,
                target_aggregation: Aggregation = None) -> Expression: ...
    def semantic(self, query: Query, input_aggregation: Aggregation = None,
                 target_aggregation: Aggregation = None) -> Expression: ...
    def map(self, fn) -> Expression: ...
    def between(self, lo, hi) -> Predicate: ...   # (self >= lo) & (self <= hi)
    def isin(self, values: list) -> Predicate: ...  # OR of == against each JSON-like value
```

`Expression` has the same methods, plus comparisons, ranking arithmetic, and
`expr[start:end]` (same as `.slice`). Methods on a `Field` open a pipeline;
methods on an `Expression` append to it.

The constructor form still works — methods are sugar over the same ops:

```python
Expression(input: Field | Expression, *operations: Operation) -> Expression

F.body.length()
Field("body").length()
Expression(Field("body"), Length())
F.body.findall(r"\w+").length()
Expression(Field("body"), RegexFindAll(r"\w+"), Length())
F.body[0:200]
```

`Value()` is identity and, if used, is first. `Lexical` / `Semantic` are last.
Each op must accept the previous op's kind.

`.map(fn)` runs `fn(cell)` in Python per entry at evaluation. Chain it, compare
it, rank it, retrieve `of=` it.

```python
def decade(value):
    n = int(value)
    return f"{(n // 10) * 10}s"

def mentions_policy(text):
    return isinstance(text, str) and "policy" in text.lower()

G0.where(F.body.map(mentions_policy) == True)
retrieve(G0, 20, of=F.year.map(decade), distinct=True)
```

A missing cell is `expr == None`.

```python
(expr == other) -> Predicate     # JSON-like value, Field, or Expression
(expr != other) -> Predicate
(expr <  other) -> Predicate     # finite number, Field, or Expression
(expr <= other) -> Predicate
(expr >  other) -> Predicate
(expr >= other) -> Predicate

(pred & pred) -> Predicate
(pred | pred) -> Predicate
(~pred) -> Predicate

Predicate(left, operator: str, right=None)
# operator: "==" "!=" "<" "<=" ">" ">=" "and" "or" "not"
```

Expression-to-expression ordering is checked when both non-missing results
are numeric.

---

## Ops

Pipeline kinds describe non-`None` values. Absence may still propagate as
`None` unless an op says otherwise. Rankable finals: `number`, `score`, and
`.map` (finite number at evaluation; anything else sorts as missing). `score`
ends the pipeline.

| Method | Op | Accepts | Produces | Result |
| --- | --- | --- | --- | --- |
| (identity) | `Value()` | `any` (first only) | unchanged | unread field |
| `.text()` | `AsText()` | any | `text` | `str(value)`; `None` → `""` |
| `.number()` | `AsNumber()` | `any`, `text`, `number`, `text_or_list` | `number` | finite `float`; `None` stays `None` |
| `.search(pattern, flags=0)` | `RegexSearch(...)` | `any`, `text`, `text_or_list` | `text` | first match (`group(0)`), or `None` |
| `.findall(pattern, flags=0)` | `RegexFindAll(...)` | `any`, `text`, `text_or_list` | `list_text` | all full matches; `None` / no match → `[]` |
| `.sub(pattern, replacement, flags=0)` | `RegexSub(...)` | `any`, `text`, `list_text`, `text_or_list` | input kind (`any` → `text_or_list`) | literal replace; per-string on `list[str]`; `None` stays `None` |
| `.slice(start, end=None)` / `[start:end]` | `Slice(...)` | `any`, `text`, `list_text`, `text_or_list` | input kind (`any` → `text_or_list`) | `[start:end]` on text or list; `None` stays `None` |
| `.length()` | `Length()` | `any`, `text`, `list_text`, `text_or_list` | `number` | `len(...)`; `None` → `0` (`int`) |
| `.lexical(...)` | `Lexical(...)` | `any`, `text`, `list_text`, `text_or_list` | `score` | FTS score (terminal) |
| `.semantic(...)` | `Semantic(...)` | `any`, `text`, `list_text`, `text_or_list` | `score` | cosine (terminal) |
| `.map(fn)` | `Map(fn)` | any non-score | `any` | `fn(cell)` |

`.search` / `.findall` need `str` or `None` at evaluation. `.number()` wants
an `int`, `float`, or numeric string.

Pipeline regex is RE2 (no lookaround, no backreferences). Flags: `re.I`,
`re.M`, `re.S`, combinable with `|`. Stdlib `re` on strings you already hold
is Python.

```python
import re
F.body.search(r"hydrangea", re.I)
F.body.findall(re.escape("C++"))
F.body.sub(r"\s+", " ", re.S)[0:200].lexical("climate")
```

---

## Groups

```python
class GroupExpr:
    scope: "entries" | "fields"

    def __init__(
        self,
        scope: str,
        *,
        predicate: Predicate | None = None,
        members: list | None = None,
        name: str | None = None,
    ) -> None: ...
    def where(self, *predicates: Predicate) -> GroupExpr: ...

G0: GroupExpr   # entries
G1: GroupExpr   # fields

(group & group) -> GroupExpr
(group | group) -> GroupExpr
(~group) -> GroupExpr
```

Exactly one of `name=` (`"G0"` with `"entries"`, `"G1"` with `"fields"`),
`predicate=` (entries only), or `members=` (`list[Entry]` or `list[Field]`
matching `scope`; `[]` is empty). `&` `|` `~` combine finished groups of the
same scope.

`where` is entry-scoped and needs at least one predicate. Several predicates
are AND. `group.where(*preds)` is `group & GroupExpr("entries", predicate=combined)`.

Intersection keeps the left group's order. Union is the left group, then
previously unseen right-side members. Complement is relative to `G0` or `G1`.
Member groups keep the given order.

```python
long = G0.where(F.body.length() >= 500)
both = G0.where(F.body.length() >= 500, F.body.lexical("hydrangea") > 0)
GroupExpr("entries", members=retrieve(long, 20))
```

---

## Ranking

```python
Ranking(expression: Expression | None = None)
```

A rankable expression (final kind `number` or `score`) used with `+` `*` `-`
is a ranking. `rank=` accepts a `Ranking` or a rankable `Expression`.
Comparisons on a ranking (`score > 0`, `rank >= 0.2`) are predicates.

```python
(rankable + rankable) -> Ranking
(rankable - rankable) -> Ranking
(rankable * weight) -> Ranking      # finite int|float >= 0, either side
(rankable < <= > >= == != other) -> Predicate
```

Empty ranking (`None` / `Ranking()`) keeps the group's order. Non-empty:
higher first; ties break by import order. Missing (`None`) or non-finite
scores sort last (`-inf`). `AsNumber` and `Semantic` produce `None` for an
absent cell. `Length` and `Lexical` never go missing: absence is `0` / `0.0`.
In a sum, any `-inf` term makes the total `-inf`. Adding an empty ranking is
a no-op.

```python
rank = F.body.lexical("hydrangea") - F.body.length() * 0.01
retrieve(long, 10, rank=rank)
retrieve(long, 10, of=rank, rank=rank)
```

`of=rank` evaluates the combined score after the slice, aligned with handles
taken under the same `group`, `rank`, `order`, and `limit`.

---

## `retrieve` and `count`

```python
retrieve(
    group: GroupExpr = G0,
    limit: int = 1,
    *,
    order: "top" | "middle" | "bottom" = "top",
    rank: Ranking | Expression | None = None,   # not with distinct=True
    of: Field | Expression | Ranking | None = None,
    distinct: bool = False,
    unit: Unit | Expression | None = None,
) -> list

count(
    group: GroupExpr = G0,
    *,
    of: Field | Expression | None = None,   # Expression only with distinct=True
    distinct: bool = False,
    unit: Unit | None = None,
) -> int
```

`retrieve(G1, 50)` and `retrieve(G1, limit=50)` are the same. `unit=` is the
explicit form; do not pass both `unit=` and `of=`.

**Infer the unit from the group.** Entry groups yield entries; field groups
yield fields. `of=` switches to values:

| Call | Items |
| --- | --- |
| `retrieve()` / `retrieve(G0)` | `Entry` (`limit` defaults to **1**) |
| `retrieve(G1, 50)` | `Field` |
| `retrieve(g, 20, of=F.body)` | present `body` cells (absent dropped **before** rank) |
| `retrieve(g, 20, of=F.body, distinct=True)` | distinct present `body` cells over the **full** group, then `limit` / `order` |
| `retrieve(g, 20, of=F.body.length())` | computed values, **after** rank and `limit` |
| `retrieve(g, 20, of=F.year.map(decade), distinct=True)` | unique computed values over the **full** group (`None` dropped), then `limit` / `order`; no `rank` |
| `retrieve(g, 10, of=rank, rank=rank)` | combined ranking scores, after the slice |
| `retrieve(unit=fields, group=G1, limit=50)` | same as `retrieve(G1, 50)` |
| `retrieve(unit=Unit("entries", Field("body")), group=g)` | same as `of=F.body` |
| `retrieve(unit=Unit("values", Field("body")), group=g)` | same as `of=F.body, distinct=True` |
| `retrieve(unit=Expression(...), group=g)` | same as `of=Expression(...)` |

A `Field` (including `F.body`) in `of=` is the present-cell path. An identity
`Expression` (no ops, or only `Value()`) is the same path. Any other
`Expression` is the computed path.

`count` sizes that population (no `limit` / `order` / `rank`). `count(g)` is
group size. `count(g, of=F.body)` is present cells; `distinct=True` counts
distinct present values. `count(g, of=expr, distinct=True)` counts unique
computed values over the full group (`None` dropped). Transforming `of=`
without `distinct=True` is retrieve-only.

**Order of work:** the group filters; present-value `of=` drops absence;
`rank` scores remaining candidates; `order`+`limit` slice; transforming
`of=` without `distinct` evaluates after the slice. `distinct=True` uniquifies
over the full group first (`None` dropped), then slices — no `rank`.

If `limit >= len(items)`, all items return.

| `order` | Slice |
| --- | --- |
| `"top"` | `items[:limit]` |
| `"bottom"` | `items[-limit:]` (order preserved) |
| `"middle"` | centered window, `start = (len(items) - limit) // 2` |

Distinctness is JSON-text identity (`1` and `1.0` are distinct), first-seen
in the group's order.

```python
class Unit:
    scope: "entries" | "fields" | "values"
    field: Field | None
    def __init__(self, scope: str, field: Field | None = None) -> None: ...

entries: Unit   # Unit("entries")
fields: Unit    # Unit("fields")
```

| `unit` | Group | Items | Rank? |
| --- | --- | --- | --- |
| `entries` / `Unit("entries")` | entries | `Entry` | yes |
| `fields` / `Unit("fields")` | fields | `Field` | no |
| `Unit("entries", field)` / `of=Field` | entries | present values | yes |
| `Unit("values", field)` / `of=Field, distinct=True` | entries | distinct values | no |
| `Expression` / transforming `of=` | entries | computed values | yes, unless `distinct=True` |

---

## `Entry`

Issued by `retrieve` or `get`. No public constructor. Unknown `get(id)`
raises `QuailFieldError`.

```python
get(id: str) -> Entry

class Entry:
    id: str
    dataset_id: str
    dataset_version_id: str
    dataset: str            # same as dataset_id on issued handles

    def __getitem__(self, field: Field | str) -> Any: ...
    def value(self, field: Field | str, default: Any = None) -> Any: ...
    def fields(self) -> list[Field]: ...
```

`get(id)` is the row with that CSV id. `entry["body"]` and `entry[F.body]`
read the cell (`None` if absent). `entry.value(field, default=)` returns
`default` when the stored value is `None`. Unknown names raise; they do not
use `default`.

`entry.fields()` is the present fields on that row (cell not `None`), catalog
order: source then analysis.

```python
row = get("n1")
print(row.id, row["body"])
```

---

## Overlay

```python
create_field(field: str | Field) -> Field
tag(group: GroupExpr | list[Entry], field: Field, value: TagValue) -> None
untag(group: GroupExpr | list[Entry], field: Field, value: TagValue | None = None) -> None
```

`create_field` makes a session-only analysis column. The name is stripped; it
returns a handle whose catalog kind is `"analysis"`. Existing analysis fields
are returned as-is. Source names are not created or overwritten.

`tag` writes `value` onto `field` for every selected entry, replacing what was
there. An empty selection writes nothing; `field` is still resolved.

`TagValue` is JSON-like with no `None` at any depth: `bool`, `int`, finite
`float`, `str`, list, or string-keyed dict.

`untag(..., field)` clears present cells. `untag(..., field, value)` clears
cells equal to `value` (Python `==`).

```python
topic = create_field("topic")
tag(interns, topic, "internship")
untag(interns, topic, "internship")
untag(interns, topic)
print(count(G0.where(F.topic == "internship")))
```

---

## Lexical and Semantic

Ordinary scores: predicates, ranking, `of=`.

Every query resolves to one or more non-empty target texts.

| Spell as | Meaning |
| --- | --- |
| `str` | One target. Lexical: FTS syntax below. |
| `list[str]` | Each string is its own query; `target_aggregation` sums (`None`/`"total"`) or means (`"avg"`). Unquoted spaces inside one string are FTS OR — a different thing. |
| Entry-scoped `GroupExpr` | For each member, read the **root field of the surrounding expression** and use that cell as a target. |
| `list[Entry]` | Same, from each listed `Entry`. |

A `list[str]` cell on a target entry expands to one target per non-empty
element. Entry-derived targets are tokenized and quoted so prose like `AND` is
not FTS syntax. `input_aggregation` combines scores across input segments for
one entry (`"avg"` divides by all segments, including unmatched).

**Lexical** is FTS relevance. `score > 0` means matched. Unmatched and absent
cells score `0.0`. Scores are corpus-relative.

**Semantic** is exact cosine under the dataset embedding profile. Cosine is
not a match bit. Absent input scores `None`. An empty string is embedded.
Larger is more similar. `"total"` sums, so aggregated scores are not unit
cosines. FTS parse rules are Lexical only — Semantic embeds the string.

Search that is not configured is a repairable `QuailRuntimeError`: configure
search, retry the whole cell.

### FTS syntax (Lexical string queries)

| Syntax | Meaning |
| --- | --- |
| Unquoted terms separated by spaces | OR (not a phrase) |
| `"quoted text"` | Adjacent tokens. No quote-escape syntax. |
| `term*` | Prefix of one punctuation-free term. `*` once, at the end. |
| Uppercase `AND` / `NOT` | Operators. `NOT` is infix with a positive left operand (`rose NOT soil`). |
| Uppercase `OR` | Not used. Separate terms with spaces. |
| Lowercase `and` / `not` / `or` | Ordinary terms |
| Punctuation | One unquoted atom with hyphens/punctuation becomes OR of the split tokens |

```python
F.body.lexical("career goals")
F.body.lexical('"career goals"')
F.body.lexical("intern*")
F.body.lexical(["intern", "fellowship"], target_aggregation="avg")
F.body.lexical(exemplars)
G0.where(F.body.lexical("hydrangea") > 0)
retrieve(G0, 10, rank=F.body.semantic("climate policy"))
```

---

## Errors

```python
class QuailError(Exception): ...
class QuailSyntaxError(QuailError): ...      # bad shape or illegal combo
class QuailScopeError(QuailError): ...       # group / unit / dataset / version pairing
class QuailFieldError(QuailError): ...       # unknown field, unknown id, or source mutation
class QuailRuntimeError(QuailError): ...     # bad cell data, search down, timeout, limit, session_busy, server_busy
```

Host failures follow `stable_error_code` and `repair_hint`.

---

## `quail_export_csv`

Host MCP tool: `quail_export_csv(session_id, dataset_id)`. Writes `"id"`,
source columns, and this session's analysis fields to a CSV on the Quail
server host. The tool result is metadata
`{path, session_id, dataset_id, dataset_version_id, columns, row_count}` —
not the file body. Overlap with `quail_exec` on the same session is
`session_busy`.
