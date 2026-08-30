# Quail Analysis API

> **Unpublished.** Not yet served by `quail_get_api_docs`.

Two questions:

```python
count(group=G0, *, of=None, distinct=False) -> int
retrieve(group=G0, limit=1, *, order="top", rank=None, of=None, distinct=False) -> list
```

`group` is who. `rank` is order. `of` is what — omit it and the members
themselves come back. Build those three in ordinary Python. **Only `print`
returns.** Recipes do not read the corpus until `retrieve`, `count`, `tag`,
`untag`, `get`, `entry[...]`, `entry.value`, or `entry.fields()`. Source data
never changes. The process has no network and no filesystem.

When a column method does not exist, in this order:

1. Write a `def` and `.map(fn)` on a column. Use it in `group`, `rank`, or
   `of`. The recipe stays lazy and bounded.
2. `retrieve` the slice and write ordinary Python on the list.
   `retrieve(g, count(g))` is all of `g`.


```text
quail_exec(session_id, dataset_id, code, time_window="standard")
```

`session_id` is the durable namespace and overlay. `dataset_id` is one dataset.
`time_window` is `"standard"` (30s wall / 15s CPU) or `"extended"` (100s / 60s);
omitted is `"standard"`. Memory is 256 MiB. Success is
`{"printed_output": "..."}`. An uncaught exception or resource limit restores
the last successful cell (no prints, no overlay, no namespace change). A
handled exception does not. Failure is a diagnostic with `error_class`,
`message`, `stable_error_code`, and optional `repair_hint`.

Pass arguments by name. One `quail_exec` or `quail_export_csv` at a time per
session (`session_busy`). Process-wide capacity exhausted is `server_busy`.
Retry the same session. After a workspace switch, start a new session.

---

## A session

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
    print(row.id, row["body"][:200])

theme = create_field("theme")
tag(both, theme, "energy-climate")
for y in retrieve(both, 20, of=F.year, distinct=True):
    print(y, count(both.where(F.year == y)))
```

`G0` is every entry (import order). `G1` is every field (source, then
analysis). Blank imported cells are `None`; a stored `""` is an empty string.
Imported CSV cells are stripped strings — `F.year.number()` for numeric
compare. Row identity is `row.id` / `get(id)`.

---

## The cell

`code` is ordinary Python: `def`, `class`, `import`, comprehensions, f-strings,
`try`, mutation. Analysis names are already bound. Standard-library imports
work; network and filesystem do not.

A successful cell’s namespace is the next cell’s starting namespace:
`Field`, `Expression`, `Predicate`, `GroupExpr`, `Ranking`, `Entry`, `def` /
`class`, imported modules, JSON-like values, and lists of those. If a
top-level name cannot persist, the cell fails. `del name` drops a name.
`.map(fn)` persists when `fn` does — write a `def`.

```python
print(*values, sep=" ", end="\n")
```

---

## `group` — who

A group is a lazy set of entries or fields. `where` is entry-scoped and needs
at least one predicate. Several predicates are AND. `&` `|` `~` combine groups
of the same scope. Intersection keeps left-hand order; union is left then new
right-hand members; complement is relative to `G0` or `G1`. A `members=` group
keeps the given order; `[]` is empty.

```python
long = G0.where(F.body.length() >= 500)
both = G0.where(F.body.length() >= 500, F.body.lexical("hydrangea") > 0)
energy & climate
energy | climate
~energy
GroupExpr("entries", members=retrieve(long, 20))
GroupExpr("fields", members=[F.body, F.year])
GroupExpr("entries", members=[])
```

```python
class GroupExpr:
    scope: "entries" | "fields"
    def __init__(self, scope: str, *, members: list) -> None: ...
    def where(self, predicate: Predicate, *more: Predicate) -> GroupExpr: ...
```

Comparisons on a column or recipe make a predicate. A missing cell is
`expr == None`. Two columns on the same row: `F.body == F.title`. Two catalog
handles: compare `.name`.

```python
(expr == != other) -> Predicate          # JSON-like value, Field, or Expression
(expr < <= > >= other) -> Predicate      # finite number, Field, or Expression
(pred & pred) -> Predicate
(pred | pred) -> Predicate
(~pred) -> Predicate
```

---

## `rank` — order

A number or score used with `+` `*` `-` is a ranking. `rank=` also accepts a
rankable expression with no arithmetic. Omit `rank=` to keep group order.
Higher first; ties break by import order.

```python
rank = F.body.lexical("hydrangea") - F.body.length() * 0.01
retrieve(long, 10, rank=rank)
retrieve(long, 10, of=rank, rank=rank)
```

Weight is a finite `int` or `float` `>= 0`, either side. Missing or non-finite
scores sort last. `Length` and `Lexical` treat absence as `0`; `AsNumber` and
`Semantic` treat it as `None`. A `-inf` term makes the total `-inf`.
Comparisons on a ranking (`score > 0`) are predicates.

`of=rank` with the same `group`, `rank`, `order`, and `limit` is the aligned
scores.

---

## `of` — what

Omit `of=` and `retrieve` / `count` talk about the group members. Pass a
column or recipe to talk about cells or computed values instead. `distinct=True`
uniquifies over the full group first (`None` dropped), then slices, and does
not take `rank`. Distinctness is JSON-text identity (`1` and `1.0` are
distinct), first-seen in group order.

| Call | Items |
| --- | --- |
| `retrieve()` | one `Entry` from `G0` |
| `retrieve(G1, 50)` | `Field` |
| `retrieve(g, 20, of=F.body)` | present cells (absent dropped before rank) |
| `retrieve(g, 20, of=F.body, distinct=True)` | unique present cells over the full group, then slice |
| `retrieve(g, 20, of=F.body.length())` | computed values after rank and slice |
| `retrieve(g, 20, of=F.year.map(decade), distinct=True)` | unique computed values over the full group (`None` dropped), then slice |
| `retrieve(g, 10, of=rank, rank=rank)` | combined scores after the slice |

A column (`F.body`) in `of=` is present cells. So is an identity
`Expression` — `Expression(Field("body"))` or
`Expression(Field("body"), Value())`. Any other recipe is computed values.

`count(g)` is group size. `count(g, of=F.body)` is present cells.
`count(g, of=F.body, distinct=True)` and `count(g, of=expr, distinct=True)`
are unique values over the full group. A computed `of=` without `distinct`
is retrieve-only. `count` takes a transforming `of=` only with `distinct=True`.
`retrieve` / `count` reject `rank=` with `distinct=True`. `rank=` and `of=`
are allowed on entry groups only.

Omitted `limit` is **1**. If `limit` is at least the population, all items
return. `order` is `"top"` (`items[:limit]`), `"bottom"`
(`items[-limit:]`, not reversed), or `"middle"`
(`start = (len(items) - limit) // 2`).

---

## Columns

```python
Field(name: str, kind: "source" | "analysis" | None = None)
F.body                 # Field("body")
F["class year"]        # Field("class year")
```

`F.body` is the column. Methods build a recipe. Chain them. The constructor
form is the same pipeline:

```python
Expression(Field("body"), Length())
Expression(Field("body"), RegexFindAll(r"\w+"), Length())
```

`Value()` is identity, and first if present. `Lexical` / `Semantic` are last.

```python
class Field:
    name: str
    kind: str | None          # "source" or "analysis" once known

    def text(self) -> Expression: ...
    def number(self) -> Expression: ...
    def length(self) -> Expression: ...
    def search(self, pattern: str, flags: int = 0) -> Expression: ...
    def findall(self, pattern: str, flags: int = 0) -> Expression: ...
    def sub(self, pattern: str, replacement: str, flags: int = 0) -> Expression: ...
    def slice(self, start: int, end: int | None = None) -> Expression: ...
    def __getitem__(self, item: slice) -> Expression: ...
    def lexical(self, query, input_aggregation=None, target_aggregation=None) -> Expression: ...
    def semantic(self, query, input_aggregation=None, target_aggregation=None) -> Expression: ...
    def map(self, fn) -> Expression: ...
    def between(self, lo, hi) -> Predicate: ...
    def isin(self, values: list) -> Predicate: ...   # OR of == against JSON-like values
```

`Expression` has the same methods.

```python
F.body.length()
F.body.search(r"hydrangea", flags=0)
F.body[0:200]
F.body.lexical("climate")
F.body == "open"
F.year.number() >= 1990
F.status.isin(["open", "closed"])
F.year.number().between(1990, 2000)    # (expr >= lo) & (expr <= hi)
```

---

## `.map(fn)`

`.map(fn)` runs `fn(cell)` in Python per entry when `retrieve`, `count`,
`tag`, or a row read evaluates the recipe. Use it for any filter, score, or
`of=` the column methods do not cover. `fn` must persist — write a `def`.

```python
def decade(value):
    return f"{(int(value) // 10) * 10}s"

def mentions_policy(text):
    return isinstance(text, str) and "policy" in text.lower()

def energy_bias(text):
    if not isinstance(text, str):
        return 0.0
    t = text.lower()
    return t.count("renewable") - t.count("coal")

G0.where(F.body.map(mentions_policy) == True)
retrieve(G0, 20, of=F.year.map(decade), distinct=True)
retrieve(G0, 10, rank=F.body.map(energy_bias))
```

---

## Ops

Each op must accept what the previous op produced. `score` ends the pipeline.
Rankable results: `number`, `score`, and `.map` when `fn` returns a finite
number (anything else sorts as missing).

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

Pipeline regex is RE2 (no lookaround, no backreferences). Flags: `re.I`,
`re.M`, `re.S`, combinable with `|`. Inline `(?i)` needs no import.
`.search` / `.findall` evaluate on `str` or `None`. `.number()` wants an
`int`, `float`, or numeric string. Stdlib `re` applies to strings you already
hold.

```python
import re
F.body.search(r"hydrangea", re.I)
F.body.findall(re.escape("C++"))
F.body.sub(r"\s+", " ", re.S)[0:200].lexical("climate")
```

---

## Rows

```python
get(id: str) -> Entry

class Entry:
    id: str
    dataset_id: str
    dataset_version_id: str
    def __getitem__(self, field: Field | str) -> Any: ...
    def value(self, field: Field | str, default=None) -> Any: ...
    def fields(self) -> list[Field]: ...
```

`get(id)` is that row (`QuailFieldError` if unknown). No public `Entry`
constructor. The CSV `id` is `row.id` / `get(id)`, not `Field("id")`.
`entry["body"]` and `entry[F.body]` are the cell (`None` if absent).
`entry.value(..., default=)` uses `default` when the stored value is `None`;
unknown names raise and ignore `default`. `entry.fields()` is the present
fields on that row, source then analysis.

```python
row = get("n1")
print(row.id, row["body"])
```

---

## Overlay

`tag` writes so the next `count` / `retrieve` can use the column as `group` or
`of`. It is not a third question.

```python
create_field(field: str | Field) -> Field
tag(group, field, value) -> None
untag(group, field, value=None) -> None
```

`create_field` makes a session analysis column (name stripped). Existing
analysis fields are returned as-is. Source names cannot be created or
overwritten.

`tag` writes `value` on every selected entry, replacing what was there.
`group` is an entry group or a `list[Entry]`. Empty selection writes nothing;
`field` is still resolved. `value` is JSON-like with no `None` anywhere:
`bool`, `int`, finite `float`, `str`, list, or string-keyed dict.

`untag(..., field)` clears the selection. `untag(..., field, value)` with a
non-`None` value clears cells equal to that value (Python `==`).

```python
topic = create_field("topic")
tag(interns, topic, "internship")
print(count(G0.where(F.topic == "internship")))
```

---

## Lexical and Semantic

A query is one or more non-empty target texts: a `str`, `list[str]`,
an entry group, or a `list[Entry]`. Entry targets read the surrounding
recipe’s root field (a `list[str]` cell becomes one target per non-empty
element) and quote their terms. `list[str]` queries are separate targets —
not the same as unquoted spaces inside one string (FTS OR).

**Lexical** is FTS. `score > 0` matched; absence and no match are `0.0`.
Scores are corpus-relative. **Semantic** is cosine under the dataset embedding
profile. Larger is more similar. Absence is `None`; empty string is embedded.
Cosine is not a match bit. `"total"` sums (not a unit cosine). FTS syntax is
Lexical only — Semantic embeds the string. Search that is not configured is a
repairable `QuailRuntimeError` — configure search and retry the cell.

`input_aggregation` combines scores across this cell’s text segments
(including `list[str]` / `.findall`). `target_aggregation` combines scores
across query targets. Each is `"total"` (default; `None` means `"total"`) or
`"avg"` (`"avg"` divides by every segment, including unmatched).

```python
F.body.lexical("career goals")
F.body.lexical('"career goals"')
F.body.lexical("intern*")
F.body.lexical(["intern", "fellowship"], target_aggregation="avg")
F.body.lexical(exemplars)
G0.where(F.body.lexical("hydrangea") > 0)
retrieve(G0, 10, rank=F.body.semantic("climate policy"))
```

| FTS (Lexical strings) | Meaning |
| --- | --- |
| Unquoted terms separated by spaces | OR |
| `"quoted text"` | Adjacent tokens (no quote-escape) |
| `term*` | Prefix; `*` once, at the end |
| Uppercase `AND` / `NOT` | Operators (`NOT` is infix: `rose NOT soil`) |
| Uppercase `OR` | Not used; use spaces |
| Lowercase `and` / `not` / `or` | Ordinary terms |
| Punctuation | Split; unquoted hyphenated atoms become OR |

---

## Names

| Kind | Names |
| --- | --- |
| Callables | `retrieve`, `count`, `create_field`, `tag`, `untag`, `get`, `print` |
| Groups | `G0`, `G1` |
| Sugar | `F` |
| Types | `Field`, `Expression`, `Predicate`, `GroupExpr`, `Ranking`, `Entry`, `Operation` |
| Ops | `Value`, `AsText`, `AsNumber`, `RegexSearch`, `RegexFindAll`, `RegexSub`, `Slice`, `Length`, `Lexical`, `Semantic`, `Map` |
| Errors | `QuailError`, `QuailSyntaxError`, `QuailScopeError`, `QuailFieldError`, `QuailRuntimeError` |

`quail_export_csv(session_id, dataset_id)` is a host tool. It writes `"id"`,
source columns, and this session’s analysis fields to a CSV on the server.
The result is `{path, session_id, dataset_id, dataset_version_id, columns,
row_count}` — a path, not the file body.
