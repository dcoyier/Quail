# Quail

You are analyzing one dataset inside a Quail session. A session is a
persistent Python kernel plus a set of tags. Each cell you send runs in that
kernel. Variables, functions, classes, and imports persist from cell to cell
while the kernel runs. Tags persist in the project on disk, outlive the
kernel, and travel with the project through git.

The dataset is an immutable grid: entries (rows) by fields (columns). You
read it with `count`, `retrieve`, and `values`. You annotate it with `tag`.
Nothing you do changes the source.

## Running cells

`quail setup --json` describes the project: this document, each dataset
with its fields, the sessions that already exist, and under `interface` the
exact commands to run next. Run it once; it starts nothing.

A session runs as one foreground process that you keep open:

```text
quail exec SESSION --stream [--dataset D] [--fork-from S]
```

Continue a session by naming it. Start a new one with a new name, adding
`--dataset` when the project has more than one. `--fork-from` starts the new
session from a copy of another session's tags and history.

The process prints one JSON line when it is ready, then answers each JSON
line you write to its stdin with one JSON line on its stdout:

```text
stdout  {"ready":true,"session":"study","run":"...","warnings":[]}
stdin   {"op":"exec","code":"n = count()\nn"}
stdout  {"session":"study","run":"...","cell":1,"output":"1204","error":null,"tags_written":0,"truncated":false,"kernel_restarted":false}
stdin   {"op":"reset"}
stdout  {"reset":true,"session":"study","run":"..."}
```

`output` is what a notebook would show: everything you `print`, then the
value of the last expression if it is not `None`, then the traceback if the
cell raised. `error` is `null` or `{"type", "message", "hint"}`. Output past
64 KiB is truncated with a note. A cell may use 30 seconds of CPU and 120
seconds of wall time; the kernel may use 1 GiB of memory.

Keep the process and send every cell through it. A new shell command per
cell is a new kernel, and your variables are gone with the old one. If your
harness runs one shell command at a time, start the stream inside a tmux
session with its stdout redirected to a file, and send lines to it. Close
stdin when you are done.

`quail exec SESSION FILE.py` runs one file as one cell in a fresh kernel and
exits. Use it for a complete saved script. Its tags persist like any cell's;
its variables do not. `quail export SESSION` writes the source fields plus
the session's tags to a CSV under `exports/`.

`warnings` in the ready record tell you when the source CSV changed since
the session last ran, or when an earlier run was interrupted mid-write (the
unfinished line is ignored; nothing else is lost). A session whose history
does not validate is reported as unavailable with the reason; other sessions
and new ones still work.

## Cells

A cell is a transaction for tags. If the cell finishes, its tag writes are
committed together and written to the session log before you see the
result. If it raises, none of them are kept. Variables you assigned before
the error are kept, as in a notebook. Fix the line and run the next cell.

Available: Python 3.12 and its standard library, and `numpy`. Already
imported for you: `re`, `math`, `statistics`, `json`, `itertools`,
`collections`, and `Counter`; import anything else as usual. Not available:
the network, the file system, subprocesses. Everything else is ordinary
Python: define functions and classes, keep results in variables, build
expressions in loops, and use them in later cells.

Two things are not ordinary:

- Expressions and predicates have no truth value. `if pred:`,
  `pred and other`, `0 < expr < 10`, and `x in Field("f")` all raise.
  Combine predicates with `&` `|` `~`; test membership with `.isin` /
  `.contains`. Verbs reject a plain `True` or `False` where they need a
  predicate.
- `is None` asks about a Python object; `== None` asks about each entry.
  `Field("topic") is None` is always `False`, because an expression is an
  object. `Field("topic") == None` is the predicate "this cell is blank".
  Use the first in helpers (`if where is None:`) and the second in queries.

## Start here

```python
fields()            # every field: name, kind ("source" or "tag"), present count
count()             # entries in the dataset
retrieve(limit=3)   # three entries in import order
```

Field names differ per dataset. Look before assuming a schema. Blank cells
are `None`. Every dataset has an `id` field.

## Expressions

`Field(name)` is the value of one column, per entry. It is the simplest
`Expression`. Every method below returns another `Expression`. Nothing is
read until a verb runs.

Source cells are text. Tag cells are whatever you wrote (`bool`, `int`,
`float`, `str`, `list`, `dict`).

| Method | Accepts | Produces | Notes |
| --- | --- | --- | --- |
| `Field(name)` | — | `text` (source) or `any` (tag) | Unknown names raise and list the fields. |
| `.text()` | any, text, number, list | `text` | Numbers and bools as JSON spells them; lists join with `"\n"`; dicts become JSON. |
| `.number()` | any, text, number | `number` | Numeric text or a number; bools are 0/1. Anything else is `None`, never an error. |
| `.length()` | any, text, list | `number` | Characters of text, items of a list, keys of a dict; `None` for a number. |
| `.lower()` `.upper()` `.strip()` | any, text | `text` | Unicode-aware. For messy categoricals before `==`. |
| `.search(pattern, flags=0)` | any, text | `text` | First regex match, or `None`. |
| `.findall(pattern, flags=0)` | any, text | `list` | Every match. |
| `.sub(pattern, repl, flags=0)` | any, text, list | same | Lists: per item. |
| `.slice(start, end=None)` | any, text, list | same | Python slice semantics. |
| `.isin(values)` | any, text, number | predicate | `values` is a list of scalars; the same as `==` against each. `[]` matches nothing. |
| `.contains(value)` | any, text, list | predicate | Substring of text, item of a list, key of a dict. |
| `.lexical(query)` | a `Field` only | `number` | Keyword relevance. See [Search](#search). |
| `.semantic(query)` | a `Field` only | `number` | Meaning similarity. See [Search](#search). |
| `Random(seed=None)` | — | `number` | A random number per entry, fixed by the seed. Use as `rank=` to sample. |

Regex patterns are RE2 syntax (no lookaround or backreferences). `flags`
accepts `re.I`, `re.M`, and `re.S` from the standard `re` module.

A method that does not accept what the previous step produces raises when
you build the expression, naming both sides. Search is only available on a
stored column: to search a transformed value, tag it first
(`tag(None, "clean", Field("body").lower())`) and search the tag field.
Nothing else about kinds needs attention.

### Absence

Absence is `None`, and it flows through everything. Every method maps `None`
to `None`. Comparisons with `None` are false, except `== None` (blank) and
`!= None` (present). Negation therefore includes blank entries:
`~(Field("topic") == "billing")` is every entry whose topic is not
`"billing"`, including entries with no topic. `None` sorts last under
`rank`. `count(by=...)` groups it under the key `None`.

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
are excluded. Two expressions compare as they are, text with text and
numbers with numbers; call `.number()` on a text column first when you mean
numbers. Ordering values of different kinds is false, never an error. Tag
values compare like Python values: `Field("labels") == ["a", "b"]` compares
lists by content.

Comparison operators: `==` `!=` `<` `<=` `>` `>=`. Predicate operators: `&`
(both), `|` (either), `~` (not). Python `and` / `or` / `not` raise.

### Arithmetic

`number` expressions combine with `+` `-` `*` `/` and unary `-`, with
literals on either side. The result is a `number` expression. Absence
propagates, and division by zero is `None`.

```python
score = Field("body").semantic("parking is hard to find") + 0.2 * Field("title").lexical("parking")
```

One expression serves as a filter (`score > 0.5`), an ordering
(`rank=score`), and a readable value (`entry[score]`). An expression is a
description, not a result: it reads the current values every time a verb
runs it, and literal arguments are copied when it is built.

## Verbs

### `count`

```python
count(where=None, by=None) -> int | Counter
```

Without `by`: how many entries match `where` (all entries when omitted).

With `by`: a `collections.Counter` from value to count, most common first
(ties in import order). `by` is an expression or a list of expressions; a
list gives tuple keys, a cross-tab. Blank values count under `None`. A
list-valued cell counts once per item and an empty list counts nothing, so
the total need not equal the entry count. A dict or nested list is keyed as
`("json", <its JSON text>)`.

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

Entries matching `where`. With `rank`, a `number` expression, highest first,
ties in import order, and `None` last; without it, import order. Negate to
sort ascending. `limit` defaults to 10 and is capped at 1000; the result
says when it was clamped. `offset` pages.

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
column to Python: `statistics`, `Counter`, `sorted`, `numpy`, your own code.
Blank cells come back as `None`; exclude them with `where=expr != None` or
in Python. Prefer `values` over `entry[expr]` in a loop.

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
entries were targeted, whether or not their value changed.

- `target`: `None` for every entry, a `Predicate`, an `Entry`, or a
  `list[Entry]`.
- `field`: a name. A tag field exists while at least one entry carries it;
  the first write creates it, clearing the last value removes it. A source
  field name is rejected. Source fields never change.
- `value`: `bool`, `int`, `float`, `str`, `list`, or `dict` (JSON-like), or
  `None` to clear, or an `Expression`, evaluated per entry.

Each call resolves its target and values first, then writes, so a predicate
that reads the field being written sees the values from before the call.
Later lines in the cell see the new values. `tag` replaces; there is no
append. For multi-label coding, either keep one boolean field per label
(`tag(p, "topic:billing", True)`) or read, extend, and rewrite the list.

```python
tag(billing, "topic", "billing")
tag(retrieve(rank=score, limit=40), "shortlist", True)
tag(None, "words", Field("body").length())
tag(Field("topic") == None, "topic", "uncoded")
tag(billing, "topic", None)                       # clear
```

Tags are scoped to this session. Another session on the same dataset does
not see them. They are the only analysis state that persists, so put
anything you want to keep in a tag, not a variable. Tagging in a Python
loop (`for e in retrieve(...): tag(e, ...)`) is fine; the writes share the
cell's transaction.

### `fields`

```python
fields() -> list[FieldInfo]
```

Every field as `FieldInfo(name, kind, present)`. `kind` is `"source"` or
`"tag"`. `present` is the number of entries with a non-`None` value.

## Entries

`retrieve` returns `Entry` objects. An `Entry` is a read-only view of one
row: its source cells and this session's tags as they are now, including
writes made earlier in the same cell.

```python
e = retrieve(limit=1)[0]
e.id                       # the same as e["id"]
e["body"]                  # a cell; None when blank; KeyError for an unknown name
e[Field("body").length()]  # any expression, evaluated for this entry
e.score                    # the rank value when retrieved with rank=, else None
dict(e), e.items(), "topic" in e
```

Printing an entry shows its cells with long text shortened and the full
length noted. Print `e.id` and specific cells when you want stable output.

## Search

Both search methods are called on a `Field` and produce a `number`
expression, so they filter, rank, and combine like any other number. The
`id` field is not searchable.

### `.lexical(query)`

Keyword relevance (BM25) of the cell against `query`. Write plain words;
wrap a phrase in double quotes to require adjacency. There are no other
operators, and a query must contain at least one word. Words are stemmed, so
`parking` matches `parked`.

The score is `None` when the cell is blank, `0` when it is present and no
query word appears, and greater than `0` when one does, so `> 0` means
matched. Scores are relative to the whole field and are not comparable
across fields or datasets.

```python
count(Field("body").lexical("parking permit") > 0)
retrieve(rank=Field("body").lexical('"front desk"'), limit=10)
```

### `.semantic(query)`

Cosine similarity between the whole cell and `query` under the dataset's
embedding model. Higher means closer in meaning. There is no match
threshold; choose one by reading results. Blank and empty cells score
`None`. A cell longer than the model accepts is an error, not a silent
truncation; when passages matter, prepare long texts into shorter rows
before import.

`query` is text. For "more like this", pass a cell: `semantic(e["body"])`.

The first semantic search on a field embeds every distinct value of that
field once. On a large dataset that can take minutes; progress is reported
on stderr. Later searches on that field, and repeated queries, reuse the
work, and vectors shared with the project through git make the first search
fast too. If the dataset has no embedding model configured, `.semantic()`
raises with a hint; `quail setup` says whether one is configured.

```python
similar = Field("body").semantic("the office closes before I finish work")
retrieve(rank=similar, limit=10)
```

Lexical and semantic scores live on different scales. When you sum them,
choose weights by reading the top results, not by assumption.

## Reusable Python

Ordinary Python is the extension mechanism. A class or function that wraps
the verbs is reusable in every later cell of the stream:

```python
class Theme:
    def __init__(self, field, query):
        self.score = Field(field).lexical(query)

    def matches(self, within=None):
        matched = self.score > 0
        return matched if within is None else matched & within

    def sample(self, within=None, limit=10):
        return retrieve(self.matches(within), rank=self.score, limit=limit)

parking = Theme("body", "parking permit")
review = parking.matches(Field("body").length() >= 20)
```

```python
# a later cell
print(count(review))
tag(review, "topic:parking", True)
parking.sample(limit=3)
```

Variables live in the kernel and die with it. Keep helper definitions you
care about in a file, and resubmit them after a restart.

## Ids and the source

`e.id` is the entry's identity: the CSV's `id` column, or the column chosen
at import. Tags are stored by id. If the CSV is edited and the session
continues, tags follow their ids: entries that were removed keep their tags
out of sight (the session reports them as orphans), entries that return get
them back, and new entries start untagged. An edited text is not
re-examined for you; the ready record warns that the source changed so you
can review the affected work.

If the CSV had no id column, ids were generated in file order
(`row-000001`, ...) and are meaningful only for that version of the file. A
session on such a dataset cannot continue once the file changes.

## Rules

1. Source is frozen. Only tags change, only in this session.
2. Absence is `None`. It flows through every method; comparisons with it
   are false except `== None` / `!= None`; it sorts last.
3. Expressions are inert. Only the verbs and `entry[...]` read data.
4. A cell commits its tags together or not at all. Variables and output
   are kept either way.
5. Expressions and predicates have no truth value. Use `&` `|` `~`, and
   `== None` for blank cells.
6. No network, no files. Otherwise it is Python.

## Errors and restarts

Every Quail error is a `QuailError` with a message and, when there is an
obvious fix, a hint. Mistakes in building an expression raise on the line
that builds it. Read the traceback, fix the cell, run again.

A cell that runs out of CPU or wall time fails with no tag writes; catching
the interrupt does not turn it into a success. If the kernel itself is
replaced (it ran out of memory, ignored the interrupt, its process died, or
you sent `reset`), the response says `kernel_restarted` or `reset`.
Variables are gone; every committed tag is intact. Resubmit your helper
definitions and continue. Nothing you sent is ever run twice on your behalf.

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
tag(None, "words", Field("body").length())
words = [w for w in values(Field("words")) if w is not None]
statistics.quantiles(words, n=4)
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
