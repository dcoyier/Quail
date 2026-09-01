# Quail

You are analyzing one dataset inside a Quail session. A session is a
persistent Python kernel plus a set of tags. Each `quail_exec` call runs one
cell in that kernel. Variables, functions, and imports persist from cell to
cell. Tags persist in the project on disk and outlive the kernel.

The dataset is an immutable grid: entries (rows) by fields (columns). You
read it with `count`, `retrieve`, and `values`. You annotate it with `tag`.
Nothing you do changes the source.

## Cells

```text
quail_exec(code, session="default", dataset=None)
```

The result is what a notebook would show: everything you `print`, then the
value of the last expression if it is not `None`, then a traceback if the
cell raised. Output past 64 KiB is truncated with a note; it is never
discarded.

A cell is a transaction for tags. If the cell finishes, its tag writes commit
together. If it raises, none of them are kept. Variables you assigned before
the error are kept, as in a notebook. Fix the line and run the next cell.

Available: Python 3.12 and its standard library. Already imported for you:
`re`, `math`, `statistics`, `json`, `itertools`, `collections`, and
`Counter`; import anything else as usual. Not available: the network, the
file system, subprocesses. Everything else is ordinary Python.

Three things are not ordinary:

- Expressions and predicates have no truth value. `if pred:`, `pred and
  other`, `0 < expr < 10`, and `x in Field("f")` all raise. Combine
  predicates with `&` `|` `~`; test membership with `.isin` / `.contains`.
- `is` and `is not` are rejected before the cell runs. Write `== None`.
- Only the verbs (`count`, `retrieve`, `values`, `tag`) and `entry[...]` read
  data. Building an expression reads nothing.

## Start here

```python
fields()            # every field: name, kind ("source" or "tag"), present count
count()             # entries in the dataset
retrieve(limit=3)   # three entries in import order
```

Field names differ per dataset. Look before assuming a schema. Blank cells
are `None`.

## Expressions

`Field(name)` is the value of one column, per entry. It is the simplest
`Expression`. Every method below returns another `Expression`. Nothing is
read until a verb runs.

Source cells are text. Tag cells are whatever you wrote (`bool`, `int`,
`float`, `str`, `list`, `dict`).

| Method | Accepts | Produces | Notes |
| --- | --- | --- | --- |
| `Field(name)` | — | `text` (source) or `any` (tag) | Unknown names raise and list the fields. |
| `.text()` | any, text, number, list | `text` | Lists join with `"\n"`; numbers become `str`. |
| `.number()` | any, text, number | `number` | Non-numeric becomes `None`, never an error. |
| `.length()` | any, text, list | `number` | Characters of text, items of a list. |
| `.lower()` `.upper()` `.strip()` | any, text | `text` | For messy categoricals before `==`. |
| `.search(pattern, flags=0)` | any, text | `text` | First regex match, or `None`. |
| `.findall(pattern, flags=0)` | any, text | `list` | Every match. |
| `.sub(pattern, repl, flags=0)` | any, text, list | same | Lists: per item. |
| `.slice(start, end=None)` | any, text, list | same | Python slice semantics. |
| `.lexical(query)` | any, text | `number` | Keyword relevance. See [Search](#search). |
| `.semantic(query)` | any, text | `number` | Meaning similarity. See [Search](#search). |
| `.isin(values)` | any, text, number | predicate | `values` is a list of literals. |
| `.contains(value)` | any, text, list | predicate | Substring of text, or membership in a list. |
| `Random(seed=None)` | — | `number` | A random number per entry. Use as `rank=` to sample. |

Regex patterns are RE2 syntax (no lookaround or backreferences). `flags`
accepts `re.I`, `re.M`, and `re.S` from the standard `re` module.

A method that does not accept what the previous step produces raises when
you build the expression, naming both sides. Nothing else about kinds needs
attention.

### Absence

Absence is `None`, and it flows through everything. Every method maps `None`
to `None`. Comparisons with `None` are false, except `== None` and
`!= None`. `None` sorts last under `rank`. `count(by=...)` groups it under
the key `None`.

### Comparisons and predicates

Comparing an expression yields a `Predicate`, a true-or-false per entry.

```python
long     = Field("body").length() >= 500
mentions = Field("body").search(r"hydrange\w+", re.I) != None
billing  = Field("topic") == "billing"
recent   = Field("year").number() >= 2024
depts    = Field("dept").lower().isin(["sales", "support"])
both     = long & mentions
either   = long | mentions
other    = ~billing
```

Comparing to a numeric literal compares numerically: `Field("age") > 30`
works on a text column, and cells that are not numbers are `None`, so they
are excluded. Compare two expressions with `Field("a") == Field("b")`.

Comparison operators: `==` `!=` `<` `<=` `>` `>=`. Predicate operators: `&`
(both), `|` (either), `~` (not). Python `and` / `or` / `not` raise.

### Arithmetic

`number` expressions combine with `+` `-` `*` `/` and unary `-`, with
literals on either side. The result is a `number` expression.

```python
score = Field("body").semantic("parking is hard to find") + 0.2 * Field("title").lexical("parking")
```

One expression serves as a filter (`score > 0.5`), an ordering
(`rank=score`), and a readable value (`entry[score]`).

## Verbs

### `count`

```python
count(where=None, by=None) -> int | Counter
```

Without `by`: how many entries match `where` (all entries when omitted).

With `by`: a `collections.Counter` from value to count, most common first.
`by` is an expression or a list of expressions (a list gives tuple keys, a
cross-tab). Absent values count under `None`. A list-valued cell counts once
per item.

```python
count(long)
count(by=Field("topic"))
count(where=long, by=[Field("dept"), Field("topic")])
count(by=Field("topic")).most_common(5)
```

### `retrieve`

```python
retrieve(where=None, rank=None, limit=10, offset=0) -> list[Entry]
```

Entries matching `where`. With `rank`, a `number` expression, highest first
and `None` last; without it, import order. Negate to sort ascending. `limit`
defaults to 10 and is capped by the project (default 1000). `offset` pages.

```python
retrieve(long, limit=5)
retrieve(rank=score, limit=20)
retrieve(where=billing, rank=-Field("body").length(), limit=3)   # shortest
retrieve(where=billing, rank=Random(seed=7), limit=10)           # a sample
```

### `values`

```python
values(expr, where=None, rank=None, limit=None) -> list
```

One computed value per matching entry, in rank order when `rank` is given,
otherwise import order. `limit=None` means all. This is how you hand a
column to Python: `statistics`, `Counter`, `sorted`, your own code. Prefer it
over `entry[expr]` in a loop, which runs one query per entry.

```python
lengths = values(Field("body").length(), where=long)
statistics.median(lengths)
values(Field("id"), rank=score, limit=50)
```

### `tag`

```python
tag(target, field, value) -> int
```

Write `value` into `field` for every targeted entry. Returns how many
entries were written.

- `target`: a `Predicate`, an `Entry`, or a `list[Entry]`.
- `field`: a name. A tag field exists while at least one entry carries it;
  the first write creates it. A source field name is rejected. Source
  fields never change.
- `value`: `bool`, `int`, `float`, `str`, `list`, or `dict` (JSON-like), or
  `None` to clear, or an `Expression`, evaluated per entry.

`tag` replaces. There is no append. For multi-label coding, either keep one
boolean field per label (`tag(p, "topic:billing", True)`) or read, extend,
and rewrite the list.

```python
tag(billing, "topic", "billing")
tag(retrieve(rank=score, limit=40), "shortlist", True)
tag(long, "words", Field("body").length())
tag(Field("topic") == None, "topic", "uncoded")
tag(billing, "topic", None)                       # clear
```

Tags are scoped to this session. Another session on the same dataset does
not see them. They are the only analysis state that persists, so put
anything you want to keep in a tag, not a variable.

### `fields`

```python
fields() -> list[FieldInfo]
```

Every field as `FieldInfo(name, kind, present)`. `kind` is `"source"` or
`"tag"`. `present` is the number of entries with a non-`None` value.

## Entries

`retrieve` returns `Entry` objects. An `Entry` is a read-only mapping from
field name to value, covering source cells and this session's tags.

```python
e = retrieve(limit=1)[0]
e.id                       # the same as e["id"]
e["body"]                  # a cell; None when blank
e[Field("body").length()]  # any expression, evaluated for this entry
e.score                    # the rank value when retrieved with rank=, else None
dict(e), e.items(), "topic" in e
```

Printing an entry shows its cells with long text shortened and the full
length noted. Print `e.id` and specific cells when you want stable output.

## Search

Both search methods produce a `number` expression, so they filter, rank,
and combine like any other number.

### `.lexical(query)`

Keyword relevance (BM25) of the cell against `query`. Write plain words;
wrap a phrase in double quotes to require adjacency. There are no other
operators. Words are stemmed, so `parking` matches `parked`.

The score is `0` when no query word appears and greater than `0` when one
does, so `> 0` means matched. Scores are relative to the whole dataset and
are not comparable across datasets or fields.

```python
count(Field("body").lexical("parking permit") > 0)
retrieve(rank=Field("body").lexical('"front desk"'), limit=10)
```

### `.semantic(query)`

Cosine similarity between the cell and `query` under the project's embedding
model. Higher means closer in meaning. There is no match threshold; choose
one by reading results. Cells longer than the model's window are scored by
their best passage.

`query` is text. For "more like this", pass a cell: `semantic(e["body"])`.

The first `.semantic()` on a field embeds every cell of that field once.
On a large dataset that can take minutes; the cell reports what it did.
Later calls on that field are fast. Tag fields work the same way.

```python
similar = Field("body").semantic("the office closes before I finish work")
retrieve(rank=similar, limit=10)
```

Lexical and semantic scores live on different scales. When you sum them,
choose weights by reading the top results, not by assumption.

## Rules

1. Source is frozen. Only tags change, only in this session.
2. Absence is `None`. It flows through every method; comparisons with it
   are false except `== None` / `!= None`; it sorts last.
3. Expressions are inert. Only the verbs and `entry[...]` read data.
4. A cell commits its tags together or not at all. Variables and output
   are kept either way.
5. Expressions and predicates have no truth value. Use `&` `|` `~`, and
   `== None`, never `is`.
6. No network, no files. Otherwise it is Python.

## Errors

Every Quail error is a `QuailError` with a message and, when there is an
obvious fix, a hint. Mistakes in building an expression raise on the line
that builds it. Read the traceback, fix the cell, run again.

If the kernel is restarted (it ran out of memory or time, or was reset), the
result says so. Variables are gone; tags are intact. Rebuild what you need
from the tags.

If you have shadowed a verb (`count = 0` is the usual accident), the
originals are available as `quail.count`, `quail.retrieve`, and so on:
`count = quail.count`.

## Example session

```python
# cell 1: look
fields()
```

```python
# cell 2: the shape of one column
count(by=Field("dept"))
```

```python
# cell 3: find a theme two ways
kw  = Field("body").lexical("parking permit lot") > 0
sem = Field("body").semantic("no place to park near the building")
print(count(kw))
for e in retrieve(rank=sem, limit=8):
    print(e.id, round(e.score, 3), e["body"][:120])
```

```python
# cell 4: code the theme, then check the coding
parking = kw | (sem > 0.55)
tag(parking, "topic", "parking")
count(by=Field("topic"))
```

```python
# cell 5: a derived number, then statistics in plain Python
tag(Field("body") != None, "words", Field("body").length())
statistics.quantiles(values(Field("words")), n=4)
```

```python
# cell 6: read the disagreements between the two signals
only_kw = kw & ~(sem > 0.55)
for e in retrieve(only_kw, limit=5):
    print(e.id, e["body"][:200])
```

If cell 4 had raised partway through, its `tag` would have been rolled back,
`parking` would still be defined, and the next cell would start from the
state after cell 3.
