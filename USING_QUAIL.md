# Using Quail

Quail is a place to study a corpus of text: survey answers, support
tickets, interview excerpts, field notes, anything worth deciding from and
too much to read end to end. You work in a persistent Python kernel, one
cell at a time, as in a notebook. You count, read, search, compare, and
write your judgments back as tags. What to look for, how to define it, and
how to check yourself are your calls; Quail makes each question cheap to
ask and each answer easy to show.

```python
body    = Field("body")
parking = body.lexical("parking permit") > 0             # keyword match, per entry
nearby  = body.semantic("no place to park near work")    # closeness in meaning, per entry

count(parking)                                           # how many
count(where=parking, by=Field("dept"))                   # and who says it
retrieve(rank=nearby, limit=5)                           # read the five closest
tag(parking | (nearby > 0.55), "topic", "parking")       # keep the decision
```

This is ordinary Python plus a small vocabulary: `Field`, comparisons that
yield a true-or-false per entry, and four verbs. `parking` and `nearby` are
descriptions, not results. They cost nothing until a verb runs them, so you
can hold them in variables, combine them, wrap them in functions and
classes, and use them again in later cells. Keyword search works on any
dataset; `.semantic()` needs an embedding model configured for it, as
described under [Search](#search).

A session has room for the whole arc of a study. Look at the fields and a
few rows. Find a theme by keyword and by meaning, and read where the two
disagree. Code entries with a scheme, check it against a random sample,
and revise it. Cross-tabulate a tag against a source field. Compute a
number per entry and hand the column to `statistics` or `numpy`. Keep a
shortlist. Fork the session to try a different scheme without disturbing
the first. Export the result, or leave the session for another agent to
continue.

## The shape of a study

- A **dataset** is an immutable grid of entries (rows) by fields (columns),
  imported once from a CSV. Every entry has a durable `id`. Nothing you do
  changes the source.
- A **session** is your workspace on one dataset: a persistent kernel plus
  the tags you have written. Sessions are named, and a dataset can have
  many.
- A **cell** is one submission to the kernel. Variables, functions,
  classes, and imports persist from cell to cell while the kernel runs.
- A **tag** is a value you write onto entries, in a field you name. Tags
  are what a session produces: they are in the session log before you see
  the cell's result, they outlive the kernel, and they travel with the
  study through git. Variables are working memory; tags are what you keep.

A study is a directory of text, and git carries it between agents and
machines. If a harness has already opened a session for you, skip to
[Cells](#cells). To start or continue a study yourself, read on.

## Working locally

Quail is installed as described in the [README](README.md#installation); a
study is a separate directory, normally its own git repository. The
commands below assume that environment is active. When a shell call does
not carry the activation, use the absolute `<checkout>/.venv/bin/quail`
path instead.

### Start a study

```sh
quail init ../study && cd ../study
cat > notes.csv <<'CSV'
id,body
n1,The parking permit is too expensive.
n2,The staff were helpful.
CSV
quail import notes.csv           # registers the dataset and builds its index
quail exec first-pass --stream   # one foreground kernel; JSON lines in, JSON lines out
```

The CSV stays where it is; import never copies or rewrites it. The stream
prints a ready record, then answers one request at a time. Send these two
cells through the same process:

```text
{"op":"exec","code":"body = Field('body')\nparking = body.lexical('parking') > 0\ncount(parking)"}
{"op":"exec","code":"tag(parking, 'topic', 'parking')\ncount(by=Field('topic'))"}
```

The first result is `1`; the second reuses `parking` and commits one tag.
Send `{"op":"close"}`, wait for the closing acknowledgment and the process
to exit, and `quail export first-pass` writes the source fields and the
session's tags to `exports/first-pass.csv`.

### Continue a study

Clone the study, enter it, and run `quail exec EXISTING_SESSION --stream`.
Indexes and tags rebuild from the text on first open; nothing is
re-imported. To choose a dataset or session first, `quail info --json`
describes the study: each dataset with its fields, the sessions that exist
with their history and last activity, the configured `limits`, and under
`interface` the exact commands to run next, as absolute invocations that
work from any shell. It starts no kernel and creates no session.

A new name starts a fresh session. To build on existing work instead,
`quail exec NEW --fork-from OLD --stream` starts `NEW` from a copy of the
tags and history of `OLD`, which must be closed and is left untouched.

### The stream

```text
quail exec SESSION --stream [--dataset D] [--fork-from S]
```

Naming an existing session continues it. A new name starts one, on the
study's only dataset or the one named by `--dataset`. The process prints
one JSON line when it is ready, then answers each JSON line on its stdin
with one JSON line on its stdout. Send one request at a time and wait for
its response:

```text
stdout  {"ready":true,"session":"study","run":"...","warnings":[],"limits":{"cpu_seconds":30,"wall_seconds":120,"memory_mb":1024,"max_limit":1000,"output_kib":64}}
stdin   {"op":"exec","code":"n = count()\nn"}
stdout  {"session":"study","run":"...","cell":1,"output":"1204","error":null,"tags_written":0,"truncated":false,"kernel_restarted":false}
stdin   {"op":"reset"}
stdout  {"reset":true,"session":"study","run":"..."}
stdin   {"op":"close"}
stdout  {"closed":true,"session":"study","run":"..."}
```

`output` is what a notebook would show: everything you print, then the
value of the last expression when it is not `None`, then the traceback if
the cell raised. `error` is `null` or an object with `type`, `message`, and
`hint`. `tags_written` is the number of entry/field pairs the cell
committed. `reset` replaces the kernel: variables are gone, tags remain.
`close` shuts the kernel down and exits; always finish with it, through a
pipe or a terminal alike (in a terminal, Ctrl-D is not end of input). A
malformed request gets one error response and the stream stays open. If
the session cannot be opened, the first line is
`{"ready":false,"error":{...}}` and the process exits nonzero.

**Limits.** By default a cell may produce 64 KiB of output and use 30 seconds
of CPU and 120 seconds of wall time, the kernel may hold 1 GiB of memory,
and `retrieve` returns at most 1000 entries per call. Output past its limit
is cut with a note and `truncated` is set. Time spent waiting for an
embedding provider does not count against the wall budget. The values are
configurable in `quail.toml`; the ready record reports those applied to
this stream, and they hold until you close it.

**Warnings.** The ready record's `warnings` tell you when the source CSV has
changed since the session last ran, or when an earlier run was interrupted
mid-write (the unfinished line is ignored; nothing else is lost). A session
whose history does not validate is reported as unavailable, with the
reason; other sessions and new ones still work.

**One file as one cell.** `quail exec SESSION FILE.py` opens the session, runs
the file as a single cell in a fresh kernel, prints the result, and exits:
zero on success, nonzero on any failure. Its tags persist like any cell's;
its variables do not. Use it for a complete saved script, or when the
harness cannot hold a process open between calls.

### Commands

Every command except `init` finds the nearest `quail.toml` in the working
directory or its parents.

| Command | Effect |
| --- | --- |
| `quail init [DIR]` | Create an empty study: a minimal `quail.toml`, `sessions/`, and `.quail/` in the ignore file. |
| `quail import CSV [--name N] [--id COL] [--embed PROVIDER/MODEL --embed-revision R]` | Register a CSV as a dataset and build its index. |
| `quail info [--json]` | Describe datasets, fields, sessions, limits, and the commands to run next. |
| `quail exec SESSION --stream [--dataset D] [--fork-from S]` | Open a session as a foreground kernel. |
| `quail exec SESSION FILE.py [--dataset D] [--fork-from S] [--json]` | Run one file as one cell and exit. |
| `quail sessions [--json]` | List sessions with their history, last activity, and availability. |
| `quail fork SRC DST` | Copy a closed session's tags and history into a new session. |
| `quail fields DATASET [--session S] [--json]` | List a dataset's fields, with a session's tag fields when one is named. |
| `quail export SESSION [--out PATH] [--json]` | Write source fields and tags to `exports/SESSION.csv`. |
| `quail warm DATASET [--field F] [--shard I/N] [--json]` | Embed a field ahead of time, alone or in shards. See [Sharing work](#sharing-work). |

## Cells

A cell is a transaction for tags. If it finishes, its tag writes are
committed together and written to the session log before you see the
result. If it raises, none of them are kept, but the variables you assigned
before the error are, as in a notebook. Fix the line and run the next cell;
a failed cell costs nothing but the time it took.

Available: Python 3.12 and its standard library, and `numpy`. Already
imported: `re`, `math`, `statistics`, `json`, `itertools`, `collections`,
and `Counter`; import anything else as usual. Not available: the network,
files, and subprocesses. Everything else is ordinary Python: define
functions and classes, keep results in variables, build expressions in
loops, and use them in later cells.

Two things are not ordinary:

- Expressions and predicates have no truth value. `if pred:`,
  `pred and other`, `0 < expr < 10`, and `x in Field("f")` all raise.
  Combine predicates with `&` `|` `~`; test membership with `.isin` and
  `.contains`. Verbs reject a plain `True` or `False` where they need a
  predicate.
- `is None` asks about a Python object; `== None` asks about each entry.
  `Field("topic") is None` is always `False`, because an expression is an
  object. `Field("topic") == None` is the predicate "this cell is blank".
  Use the first in helpers (`if where is None:`) and the second in queries.

## First look

```python
fields()            # every field: name, kind ("source" or "tag"), present count
count()             # entries in the dataset
retrieve(limit=3)   # three entries in import order
```

Field names differ per dataset; look before assuming a schema. Blank cells
are `None`. Every dataset has an `id` field.

## Expressions

`Field(name)` is the value of one column, per entry. It is the simplest
`Expression`, and every method below returns another. Nothing is read
until a verb runs.

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
| `.semantic(query)` | a `Field` only | `number` | Closeness in meaning. See [Search](#search). |
| `Random(seed=None)` | — | `number` | A random number per entry, fixed by the seed. Use as `rank=` to sample. |

Regex patterns are RE2 syntax (no lookaround or backreferences). `flags`
accepts `re.I`, `re.M`, and `re.S` from the standard `re` module.

A method that does not accept what the previous step produces raises when
you build the expression, naming both sides. Search is only available on a
stored column: to search a transformed value, tag it first
(`tag(None, "clean", Field("body").lower())`) and search the tag field.
Nothing else about kinds needs attention.

### Absence

Absence is `None`. Value-producing methods propagate it; predicates always
return booleans. Comparisons involving absence are false, except `== None`
(blank) and `!= None` (present). `.isin([None])` also matches blank cells;
`.contains(...)` is false for them. Negation therefore includes blank
entries: `~(Field("topic") == "billing")` is every entry whose topic is not
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

Four verbs read and write the dataset, and `fields()` describes it.

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
sort ascending. `limit` defaults to 10 and is capped by the configured
`max_limit` (default 1000); cell output notes when it was clamped. `offset`
pages.

```python
retrieve(long, limit=5)
retrieve(rank=score, limit=20)
retrieve(where=billing, rank=-Field("body").length(), limit=3)   # shortest
retrieve(where=billing, rank=Random(seed=7), limit=10)           # a sample
```

A seeded `Random` gives the same sample every time, which makes it a fair
way to check your own coding: read ten entries you tagged and see whether
you agree with yourself.

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
tag(None, "characters", Field("body").length())
tag(Field("topic") == None, "topic", "uncoded")
tag(billing, "topic", None)                       # clear
```

Tags are scoped to this session; another session on the same dataset does
not see them. They are the only analysis state that persists, so put
anything you want to keep in a tag, not a variable. A provisional label is
fine: tag now, read a sample, and rewrite. Tagging in a Python loop
(`for e in retrieve(...): tag(e, ...)`) is fine too; the writes share the
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
operators, and a query must contain at least one word. Words are stemmed,
so `parking` matches `parked`.

The score is `None` when the cell is blank, `0` when it is present and none
of the query's words appear, and greater than `0` when any of them does, so
`> 0` means matched. Higher is a better match: more of the query's words,
and rarer ones. Scores are relative to the whole field and are not
comparable across fields or datasets.

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

`query` is text. For "more like this", pass a cell:
`Field("body").semantic(e["body"])`.

```python
similar = Field("body").semantic("the office closes before I finish work")
retrieve(rank=similar, limit=10)
```

Semantic search needs an embedding model, chosen for the dataset at import
(`--embed ollama/embeddinggemma --embed-revision v1`) or in `quail.toml`.
The revision is a label you give the model's current weights; change it
when they change, and vectors from the two are kept apart. Without a model,
`.semantic()` raises with a hint, and `quail info` says whether one is
configured.

The first semantic search on a field embeds every distinct value of that
field once. On a large dataset that can take minutes; progress is reported
on stderr. Later searches on that field, and repeated queries, reuse the
work, and vectors shared with the study through git make the first search
fast too (see [Sharing work](#sharing-work)).

Lexical and semantic scores live on different scales. When you sum them,
choose weights by reading the top results, not by assumption.

## Reusable Python

Ordinary Python is the extension mechanism. Anything you would write in a
notebook works here and is reusable in every later cell of the stream: a
class or function that wraps the verbs, a coding scheme kept as a dict of
predicates, a loop that tags entry by entry.

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
care about in a script in the study, and resubmit them as a cell after a
restart.

## An example session

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
for e in retrieve(where=sem != None, rank=sem, limit=8):
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
tag(None, "characters", Field("body").length())
lengths = [n for n in values(Field("characters")) if n is not None]
statistics.quantiles(lengths, n=4)
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

## Ids and source edits

`e.id` is the entry's identity: the CSV's `id` column, or the column chosen
with `--id` at import. Tags are stored by id. If the CSV is edited and the
session continues, tags follow their ids: entries that were removed keep
their tags out of sight (the session reports them as orphans), entries that
return get them back, and new entries start untagged. Edited text is not
re-examined for you; the ready record warns that the source changed so you
can review the affected work.

If the CSV had no id column, ids were generated in file order
(`row-000001`, ...) and are meaningful only for that version of the file.
To continue a session across edits, first write those original ids into an
explicit `id` column, then edit or reorder; tags follow the stable values.
Numbering rows after reordering does not preserve identity. While ids
remain generated, an existing session opens only against its original
source version.

## Sharing work

Locally, a study is a directory you commit and share with your own git
tools: the manifest, the source CSVs, the session logs, and any `warm/`
files. `.quail/` holds disposable indexes and local locks and stays
gitignored. Quail never runs git.

Sessions are the unit of parallel work. Two agents in two sessions push
separate log files and merge without conflict; an agent continuing
another's session appends a new log file to it. Two agents may even
continue the same session on separate machines: git merges their files,
and Quail replays them in one fixed order, but it does not reconcile their
disagreements. Give independent coding passes their own sessions, or fork
one from the other, and compare the exports.

Semantic vectors can be shared the same way. `quail warm notes --field body`
embeds a field before anyone searches it; with `--shard 1/4` through `4/4`,
four workers each embed a quarter and commit the resulting `warm/` files. A
fresh clone uses whatever parts have arrived, and ordinary search fills in
the rest. Warming is preparation, never a requirement.

## Errors and restarts

Every Quail error is a `QuailError` with a message and, when there is an
obvious fix, a hint. Mistakes in building an expression raise on the line
that builds it. Ordinary Python exceptions keep their type and message.
For a normal cell error, read the traceback, fix the cell, and run again.

A cell that runs out of CPU or wall time fails with no tag writes; catching
the interrupt does not turn it into a success. If the kernel itself is
replaced (it ran out of memory, ignored the interrupt, its process died, or
you sent `reset`), the response says so with `kernel_restarted` or `reset`.
Variables are gone; every committed tag is intact. Resubmit your helper
definitions and continue. Nothing you sent is ever run twice on your behalf.

Losing the stream, or receiving a host persistence error, is different: a
cell may have committed before its response reached you. If the process is
still running, keep reading its pending response. Otherwise reopen the
session to recover its committed history, and look for the cell's record
under `sessions/SESSION/log/` by its reported run, cell number, and code
before deciding whether to resubmit. An unanswered cell is not necessarily
a failed cell.

If you have shadowed a verb (`count = 0` is the usual accident), the
originals are available as `quail.count`, `quail.retrieve`, and so on:
`count = quail.count`.

## Always true

Six facts hold everywhere in Quail:

1. The source is frozen. Only tags change, and only in this session.
2. Absence is `None`. Value methods propagate it; predicates return
   booleans; comparisons with it are false except `== None` and `!= None`;
   it sorts last.
3. Expressions are inert. Only the verbs and `entry[...]` read data.
4. A cell commits its tags together or not at all. Variables and output
   are kept either way.
5. Expressions and predicates have no truth value. Use `&` `|` `~`, and
   `== None` for blank cells.
6. No network, no files, no subprocesses. Otherwise it is Python.
