# Using Quail

This document assumes you've read [`README.md`](README.md). Go back
and do so if you haven't.

Quail is meant to give you freedom to *explore*. If you're an agent, try to really take
advantage of that freedom unless otherwise specified. In Quail, you work in a persistent
Python kernel, one cell at a time, as in a notebook. You can count, read, search, and
compare data with the analysis language, and you can also save results as tags.

Backed by Python and the analysis language, a session has capacity for the
whole arc of a study. Here's one example:

1. Look at the fields and a few rows.
2. Find a theme by keyword and by meaning, and discover where the two
  disagree.
3. Code entries with a scheme, check it against a random sample,
  and revise it.
4. Cross-tabulate a tag against a source field. Compute a
  number per entry and hand the column to `statistics` or `numpy`.
5. Fork the session to try a different scheme without disturbing
  the first. Export the result, or leave the session for another agent to
  continue directly.

Regarding this example, it crucially is just **one** way to navigate inside
Quail; the point of Quail is never to constrain you to a single workflow such as this.
It is to provide the medium to *explore* and journey through the data.

Now let's dive deeper.

## Studies, datasets, and sessions

Here's some vocab, reiterating some core details from the [`README.md`](README.md).

- A **dataset** is an immutable grid of entries (rows) by fields (columns),
  imported once from a CSV. Every entry has a durable `id`, and nothing you do
  changes the source.
- A **session** is your persistent workspace on one dataset, with its tags
  and recorded analysis history. Sessions are named, and a dataset can have
  many. Each local copy of a session has at most one live Python kernel,
  but can have successive kernel runs as you close, reopen, or reset it.
- A **cell** is one block of code submitted to the kernel. Variables, functions,
  classes, and imports persist from cell to cell while the kernel runs.
- A **tag** is a value you write onto entries, in a field you name. Tags
  are durable annotations: they are in the session log before you see
  the cell's result, they outlive the kernel, and they travel with the
  study through git. The log also preserves submitted code and captured output.

At a high level, a study is a directory of text, and git can carry it between agents and
machines.

Here's an example of a study on disk:

```text
my-study/
  quail.toml
  notes.csv
  sessions/first-pass/session.toml
  sessions/first-pass/log/20260901T210000Z-<uuid>.jsonl
  exports/first-pass.csv                                       # from quail export (command explained later)
  warm/notes/<source-version>/<plan>/part-0001-of-0004.jsonl   # optional shared vectors
  .quail/                                                      # derived index and locks, gitignored
```

## Working locally

Quail can be installed as described in the [README](README.md#installation); a
study is a separate directory, usually its own git repository. The
commands below assume that environment is active. When a shell call does
not carry the activation, use the absolute `<checkout>/.venv/bin/quail`
path instead.

### Start a study (example)

```sh
quail init ../study && cd ../study
cat > notes.csv <<'CSV'
id,body
n1,The parking permit is too expensive.
n2,The staff were helpful.
CSV
quail import notes.csv           # registers the dataset and builds its index
```

The CSV stays where it is; import never copies or rewrites it. Submit these
two cells as separate commands:

```sh
quail exec first-pass -c 'body = Field("body"); parking = body.lexical("parking") > 0; count(parking)'
quail exec first-pass -c 'tag(parking, "topic", "parking"); count(by=Field("topic"))'
```

The first result is `1`; the second reuses `parking` and commits one tag.
Each command exits after its result, and the kernel stays alive between them.
You can export the source fields and tags to `exports/first-pass.csv`, then close
the session:

```sh
quail export first-pass
quail exec first-pass --close
```

### Continue a study

To continue a study, clone it, enter its directory, and run
`quail exec EXISTING_SESSION -c 'fields()'`.
Indexes and tags rebuild from the text files on this first open.
There is no need to run `init` or re-import its CSVs.
To choose a dataset or session first, `quail info --json` describes the
study's datasets and sessions and gives exact commands to run next.
Inspection starts no kernel and creates no session.

A new name starts a fresh session. To build on existing work instead,
`quail exec NEW --fork-from OLD -c 'fields()'` starts `NEW` from a copy of the
tags and history of `OLD`, which must be closed and is left untouched.

### Executing cells

```text
quail exec SESSION -c CODE [--dataset D] [--fork-from S] [--json]
quail exec SESSION FILE.py [--dataset D] [--fork-from S] [--json]
```

**One file as one cell.** `quail exec SESSION FILE.py` reads the file as UTF-8
and submits it to the same session kernel as `-c`. Its variables and helpers
remain available to later commands.

Naming an existing session continues it. A new name starts one, on the
study's only dataset or the one named by `--dataset`. Each command submits
one cell, prints its result, and exits. Quail starts the session's local
host when needed and keeps its Python kernel alive between commands.
Variables, functions, and classes remain available until the kernel is
reset, closed, or lost; tags survive those events.

Wait for each command's result before submitting the next. If the harness
backgrounds a long command, use its normal wait/output tool to finish
reading that command. A competing exec, reset, or close fails as busy;
inspection remains available while a cell runs.

`quail exec SESSION --reset` replaces the kernel: variables are gone, tags
remain. It requires an existing session and starts its kernel if stopped.
`quail exec SESSION --close` shuts the host and kernel down and releases
their resources; finish with it when done. Closing an already-stopped session
succeeds without starting one. A kernel has no automatic idle expiry.

### Results and status

Stdout is what a notebook would show: everything you print, then the value
of the last expression when it is not `None`, then the traceback if the cell
raised. The command exits zero on success and nonzero on failure. An ordinary
cell error leaves the kernel usable. With `--json`, stdout is one result
object instead:

```json
{"session":"study","run":"...","cell":1,"output":"1204","error":null,"tags_written":0,"truncated":false,"kernel_restarted":false,"warnings":[],"limits":{"cpu_seconds":30,"wall_seconds":120,"memory_mb":1024,"max_limit":1000,"output_kib":64}}
```

`output` contains that notebook text. `error` is `null` or an object with
`type`, `message`, and `hint`. `tags_written` counts the entry/field pairs
committed by the cell. Warnings and host progress appear on stderr too.

**Inspection.** `quail info --json` describes each dataset with its fields,
the sessions that exist with their history and last activity, the configured
`limits`, and under `interface` the exact commands to run next, as absolute
invocations that work from any shell. Session listings also show local
runtime state: stopped, idle, busy, or unavailable with a reason. Live status
includes the active run/cell, latest completed cell, and applied limits.

**Limits.** By default a cell may produce 64 KiB of output and use 30 seconds
of CPU and 120 seconds of wall time, the kernel may hold 1 GiB of memory,
and `retrieve` returns at most 1000 entries per call. Output past its limit
is cut with a note and `truncated` is set. Time spent waiting for an
embedding provider does not count against the wall budget. The values are
configurable in `quail.toml`; execution results and live status report the
applied values, which hold until you close the host.

**Warnings.** Execution results' `warnings` report a fresh kernel start,
source changes since the session last ran, or an earlier run interrupted
mid-write. An unfinished final line is ignored; earlier complete records
apply if the history validates. A session whose history does not validate
is reported as unavailable, with the reason; other sessions and new ones
still work. For recovery after errors or a lost response, see
[Errors and restarts](#errors-and-restarts).

### Commands

Every command except `init` finds the nearest `quail.toml` in the working
directory or its parents.

| Command | Effect |
| --- | --- |
| `quail init [DIR]` | Create an empty study: a minimal `quail.toml`, `sessions/`, and `.quail/` in the ignore file. |
| `quail import CSV [--name N] [--id COL] [--embed PROVIDER/MODEL --embed-revision R]` | Register a CSV as a dataset and build its index. |
| `quail info [--json]` | Describe datasets, fields, sessions and local runtime state, limits, and the commands to run next. |
| `quail exec SESSION -c CODE [--dataset D] [--fork-from S] [--json]` | Run one cell, starting the session's host if needed. |
| `quail exec SESSION FILE.py [--dataset D] [--fork-from S] [--json]` | Run a file as one cell on the same persistent kernel. |
| `quail exec SESSION --reset [--json]` | Replace an existing session's kernel; retain tags. |
| `quail exec SESSION --close [--json]` | Stop the session's local host and kernel. |
| `quail sessions [--json]` | List sessions with their history, last activity, availability, and local runtime state. |
| `quail fork SRC DST` | Copy a closed session's tags and history into a new session. |
| `quail fields DATASET [--session S] [--json]` | List a dataset's fields, with a session's tag fields when one is named. |
| `quail export SESSION [--out PATH] [--json]` | Write source fields and tags to `exports/SESSION.csv`. |
| `quail warm DATASET [--field F] [--shard I/N] [--json]` | Embed a field ahead of time, alone or in shards. See [Sharing work](#sharing-work). |

## Cells

A cell is a transaction for tags. If it finishes, its tag writes are
committed together and written to the session log before you see the
result. If it raises normally, none of its tag writes are kept, but Python
assignments and mutations made before the error remain, just like a notebook.
Fix the error with that working state in mind. Completed embedding work
may remain cached even when the cell fails.

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

The code blocks in the reference below illustrate individual operations.
They use example fields and may reuse expressions from earlier snippets;
adapt them to your dataset. [An example session](#an-example-session) gives
a continuous walkthrough with numbered cells.

## Expressions

`Field(name)` is the value of one column, per entry. It is the simplest
`Expression`, and every method below returns another.

Expressions are descriptions, not results. Constructing them does not read
entry values or search the dataset. You can hold them in variables, combine
them, wrap them in functions and classes, and use them again in later cells.
A verb or an entry lookup (`entry[expr]`, explained under [Entries](#entries))
evaluates them.

Keyword search with `.lexical()` works on any dataset; `.semantic()` needs an
embedding model configured for it, as described under [Search](#search).

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
description, not a result: it reads the current values each time it is
evaluated, and literal arguments are copied when it is built.

## Verbs

Four verbs query the dataset and write session tags; `fields()` describes
the available fields.

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

A fixed seed makes a sample repeatable for the same data and query. To
check a coding scheme, sample both included and excluded entries: the first
can reveal incorrect labels, the second missed matches. Use fresh samples
as you revise the scheme.

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
not see them. Tags survive kernel loss; variables are working memory. A
provisional label is fine: tag now, read a sample, and rewrite. Tagging in a
Python loop (`for e in retrieve(...): tag(e, ...)`) is fine too; the writes
share the cell's transaction.

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

The score is `None` when the cell is blank, `0` for a present nonmatch,
and greater than `0` when any unquoted query word or complete quoted phrase
matches, so `> 0` means matched. Higher is a better match under BM25. Scores
are relative to the whole field and are not comparable across fields or
datasets.

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

The first semantic search on a field embeds every distinct value of that
field once. On a large dataset that can take minutes; progress is reported
on stderr. Later searches on that field, and repeated queries, reuse the
work, and vectors shared with the study through git make the first search
fast too (see [Sharing work](#sharing-work)).

Lexical and semantic scores live on different scales. When you sum them,
choose weights by reading the top results, not by assumption.

### Configuring semantic search

Semantic search needs an embedding model. Locally, choose one at import
(`--embed ollama/embeddinggemma --embed-revision v1`) or in `quail.toml`.
For an existing dataset, edit its table; keep its source and ID settings.
For example, the `notes` dataset from the first study becomes:

```toml
[datasets.notes]
source = "notes.csv"
embed = "ollama/embeddinggemma"
embed_revision = "v1"
```

Import registers new datasets; do not re-import `notes` or add a second
`[datasets.notes]` table. This example uses Ollama at `http://127.0.0.1:11434`.
For another Ollama server, set `base_url` under `[providers.ollama]`.

For an OpenAI-compatible endpoint, set the dataset's `embed` to
`"openai/MODEL"`, replacing `MODEL` with its model name, and add or edit:

```toml
[providers.openai]
base_url = "https://your-provider.example/v1"
api_key = "env:OPENAI_API_KEY"
```

Replace the URL with the endpoint's base URL. Export `OPENAI_API_KEY` before
starting the session; keep only the environment reference in the manifest.
Omit `api_key` if the endpoint needs no credentials. The selected model must
be available at its provider when missing embeddings are needed.

The revision labels fixed weights and embedding behavior, including
preprocessing. Change it when those change; vectors from different revisions
are kept apart. After configuration changes, close the session with
`quail exec SESSION --close`; the next exec opens it with the new settings.
Reset retains a live kernel's configuration. Without an embedding
configuration, `.semantic()` raises with a hint, and `quail info` says
whether one is configured.

## Reusable Python

Ordinary Python is the extension mechanism, with the libraries and
capability restrictions described under [Cells](#cells). You can reuse your code in
later cells of the kernel: a class or function that wraps the verbs, a
coding scheme kept as a dict of predicates, a loop that tags entry by entry.

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

Suppose you are exploring parking concerns in a staff survey. The following
cells assume an open session with survey responses in `body`, a `dept`
field, and [semantic search configured](#configuring-semantic-search).

Begin with a first look at the dataset: its fields, size, and a few entries.

```python
# cell 1: look
print(fields())     # every field: name, kind ("source" or "tag"), present count
print(count())      # entries in the dataset
retrieve(limit=3)   # three entries in import order
```

Field names differ per dataset; look before assuming a schema. Blank cells
are `None`. Every dataset has an `id` field.

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
# cell 4: code the theme, then inspect the counts
# 0.55 is illustrative; choose a cutoff for this corpus and model after inspection.
parking = kw | (sem > 0.55)
tag(parking, "topic", "parking")
count(by=Field("topic"))
```

```python
# cell 5: read the disagreements between the two signals
only_kw = kw & ~(sem > 0.55)
only_sem = (sem > 0.55) & ~kw
for e in retrieve(only_kw, limit=5):
    print("keyword only", e.id, e["body"][:200])
for e in retrieve(only_sem, limit=5):
    print("semantic only", e.id, e["body"][:200])
```

```python
# cell 6: a derived number, then statistics in plain Python
tag(None, "characters", Field("body").length())
lengths = [n for n in values(Field("characters")) if n is not None]
statistics.quantiles(lengths, n=4)
```

If cell 4 raised normally after assigning `parking`, the tags would return
to their state after cell 3, while `parking` would remain defined.

## Ids and source edits

`e.id` is the entry's identity: the CSV's `id` column, or the column chosen
with `--id` at import. Tags are stored by id. If the CSV is edited and the
session continues, tags follow their ids: entries that were removed keep
their tags out of sight (the session reports them as orphans), entries that
return get them back, and new entries start untagged. Edited text is not
re-examined for you; opening warns that the source changed so you
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
files. `.quail/` holds disposable indexes, local locks, and runtime sockets
and stays gitignored. Quail never runs git.

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

Before editing source CSVs or pulling changes to source, logs, or `warm/`
files, close affected sessions with `quail exec SESSION --close`.
The next exec opens the updated study. A live kernel keeps its source
snapshot and synchronized history; new pack paths are discovered on open.
Reset retains that source snapshot and configuration, so use close and
reopen to synchronize. Python variables are gone after reopening; resubmit
saved helper definitions.

## Errors and restarts

Every Quail error is a `QuailError` with a message and, when there is an
obvious fix, a hint. Mistakes in building an expression raise on the line
that builds it. Ordinary Python exceptions keep their type and message.
For a normal cell error, read the traceback, fix the cell, and run again.

A cell that runs out of CPU or wall time fails with no tag writes; catching
the interrupt does not turn it into a success. If the kernel itself is
replaced (it ran out of memory, ignored the interrupt, its process died, or
you reset it), the result reports the replacement in diagnostics and in
JSON through `kernel_restarted` or `reset`.
Variables are gone; every committed tag is intact. A hard process death can
also lose output still buffered in the kernel. Resubmit your helper
definitions and continue. Nothing you sent is ever run twice on your behalf.

Losing a command's response, or receiving a host persistence error, is
different: an accepted cell can still be running or already committed.
Interrupting the client does not cancel the cell. If the client is still
running, keep reading its eventual result. Otherwise use `quail info --json`
to check local runtime state; a live host reports its current or latest
completed run/cell.
Look for the corresponding record under `sessions/SESSION/log/`, checking
its run, cell number, and submitted code before deciding whether to resubmit.
Completed results remain in that log even when their client disappeared.
A stopped host's next exec restores tags into a fresh kernel and reports
the empty Python working state. An unanswered cell is not necessarily a
failed cell; Quail never automatically repeats it.

If you have shadowed a verb (`count = 0` is the usual accident), the
originals are available as `quail.count`, `quail.retrieve`, and so on:
`count = quail.count`.

## Always true

Six facts hold everywhere in Quail:

1. The source is frozen. Only tags change, and only for the session.
2. Absence is `None`. Value methods propagate it; predicates return
   booleans; comparisons with it are false except `== None` and `!= None`;
   it sorts last.
3. Expressions are inert. Only the verbs and `entry[...]` read data.
4. A cell commits its tags together or not at all. Ordinary exceptions keep
   Python assignments, mutations, and captured output; kernel replacement
   discards working memory.
5. Expressions and predicates have no truth value. Use `&` `|` `~`, and
   `== None` for blank cells.
6. No network, no files, no subprocesses. Otherwise it is Python.

Go *explore*!
