# Storage

How a Quail project is laid out on disk, what is durable, what is derived,
and how the language in [`api.md`](api.md) compiles onto it.

## The one idea

A project is a directory of plain text, and git is the transport. The CSV,
the manifest, and every session's log are text files that agents commit and
push like any other source. The SQLite index that makes queries fast is
derived from those files and is never committed. A second machine clones
the project, and Quail rebuilds the index from what it finds.

This is what makes sessions portable between a local agent and a subagent
on its own VM without a server, a sync protocol, or a shared database. It
also gives every tag a history: the cell that wrote it, in the run that
executed it, in the commit that pushed it.

## Layout

```text
my-study/
  quail.toml                     # manifest: datasets, providers, kernel limits
  data/notes.csv                 # source; text; committed
  sessions/
    billing-coding/
      session.toml               # dataset, created, forked_from, description
      log/
        20260901T210000Z-a1b2c3.jsonl   # one file per kernel run; append-only
        20260902T083000Z-9f0e11.jsonl
  exports/                       # CSVs written by quail_export; committed if wanted
  .quail/                        # derived; gitignored
    notes.quail                  # SQLite index for dataset "notes"
  .gitignore                     # .quail/ and sessions/*/.lock
```

Durable and committed: `quail.toml`, `data/`, `sessions/`. Derived and
ignored: `.quail/`. Quail never runs git. Agents commit and push with their
own tooling; Quail only has to make sure that what they commit is complete
and that what they pull is honored.

## Manifest: `quail.toml`

```toml
[project]
quail = "1"                         # manifest schema version

[datasets.notes]
source = "data/notes.csv"
id = "id"                           # column holding unique entry ids; omit to synthesize
embed = "ollama/embeddinggemma:latest"   # optional; provider/model for .semantic()

[providers.ollama]                  # optional; these are the defaults
base_url = "http://127.0.0.1:11434"

[providers.openai]                  # any OpenAI-compatible embeddings endpoint
base_url = "https://api.openai.com/v1"
api_key = "env:OPENAI_API_KEY"      # always env:NAME, never a literal

[kernel]                            # optional; these are the defaults
cpu_seconds = 30
wall_seconds = 120
memory_mb = 1024
max_limit = 1000
output_kib = 64
```

`quail init` writes the skeleton and `quail import` adds a `[datasets.*]`
table. The file is meant to be hand-edited too. Unknown keys are an error.
Paths are relative to the manifest. Embedding dimensions are not declared;
they are learned from the first response and recorded in the index.

Without `embed`, `.semantic()` raises with the hint to add it. `.lexical()`
needs nothing.

## Datasets

`quail import data/notes.csv` reads a UTF-8 CSV with a header row and
writes the index. Every cell is text; an empty cell is absent (`NULL`). No
type inference: `"00501"` stays `"00501"`, and numeric comparison happens at
query time by the rule in `api.md`.

The `id` column must be unique and non-empty. If the manifest names no id
column and the CSV has none, ids are synthesized as `row-000001` in file
order, and the import prints a warning, because tags reference entries by
id: a CSV without stable ids cannot be re-imported without orphaning tags.

Re-import happens automatically when the CSV's content hash differs from
the one recorded in the index. It rebuilds `entries`, `entries_fts`, and
the source `chunks`; it keeps `vectors` (content-addressed, so unchanged
text costs nothing) and re-derives every session's tags from its logs. Tags
whose entry id no longer exists stay in the log and are reported as orphans
by `quail sessions`.

## Sessions

A session is a name, a dataset, and a log. `sessions/<name>/session.toml`:

```toml
dataset = "notes"
created = "2026-09-01T21:00:00Z"
forked_from = "exploration"          # optional
description = "Code parking complaints by cause"   # optional
```

### The log

`sessions/<name>/log/<run-id>.jsonl` is the durable record of everything
that happened in one kernel run. A run id is `<UTC timestamp>-<6 hex>`,
for example `20260901T210000Z-a1b2c3`. Each kernel process writes exactly
one log file and only appends to it. Two agents therefore never write the
same file, which is why git merges sessions without conflicts.

The first line is the run header. Every later line is one cell:

```json
{"run":"20260901T210000Z-a1b2c3","started":"2026-09-01T21:00:00Z","actor":"vm-7","quail":"1.0.0","dataset":"notes","source_hash":"sha256:4f1c…"}
{"n":1,"started":"…","ended":"…","code":"fields()","output":"[FieldInfo(name='id', …)]","error":null,"tags":[]}
{"n":2,"started":"…","ended":"…","code":"tag(kw, \"topic\", \"parking\")\ncount(by=Field(\"topic\"))","output":"Counter({'parking': 412, None: 4400})","error":null,"tags":[{"field":"topic","values":{"r17":"parking","r18":"parking"}}]}
{"n":3,"started":"…","ended":"…","code":"retrieve(rank=sem, limit=10)","output":"","error":{"type":"NameError","message":"name 'sem' is not defined","hint":null},"tags":[]}
```

Fields of a cell record:

| Field | Meaning |
| --- | --- |
| `n` | Cell number within this run, from 1. |
| `started`, `ended` | RFC 3339 UTC. |
| `code` | The cell exactly as submitted. |
| `output` | The cell result as returned to the agent, after truncation. |
| `error` | `null`, or `{type, message, hint}`. Failed cells are logged; they have no tag writes. |
| `tags` | The tag writes this cell committed, one object per field touched: `{"field": name, "values": {entry_id: value_or_null}}`. `null` is a clear. Entry ids, never row numbers. |

`actor` in the header comes from the `QUAIL_ACTOR` environment variable,
defaulting to the hostname. It is provenance, not identity.

### Replay

The tags of a session are a pure function of its log files:

1. Read every `log/*.jsonl`. Ignore lines that fail to parse, and report
   them.
2. Collect all cell records with `error == null`. Sort by `started`, then
   `run`, then `n`.
3. Apply each record's `tags` in order. For each `(entry_id, field)` the
   last write wins; `null` deletes.

Clocks on different machines disagree by seconds, so ordering between
concurrent runs is approximate. That is acceptable: two agents writing the
same `(entry, field)` from different machines is a coding disagreement, not
a race, and the log preserves both writes for anyone who wants to look.

### Merging and forking

Merging is `git merge`. Log files have distinct names, so the union of two
histories is the union of their runs, and replay produces the merged
tags. `session.toml` can conflict only if both sides edit the same key.

Forking is a copy: `quail fork exploration billing-coding` copies `log/`
and writes a new `session.toml` with `forked_from`. The copied run files
keep their names, so the new session's history shows where it came from.
Over MCP, `quail_exec(code, session="billing-coding", fork_from="exploration")`
does the same on first use of a new name.

### Locks

One kernel per session per machine. `sessions/<name>/.lock` holds an
advisory `flock` while a kernel is open; a second opener fails with a clear
error naming the session. Two agents on one machine work in two sessions.
The lock file is gitignored.

## The index: `.quail/<dataset>.quail`

One SQLite file per dataset, WAL mode, `busy_timeout` of five seconds.
Deleting it is always safe.

```sql
CREATE TABLE meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);  -- schema_version, source_hash, id_column, columns (JSON), embed_model, embed_dims, imported_at

CREATE TABLE entries (
  rowid INTEGER PRIMARY KEY,
  "id" TEXT NOT NULL UNIQUE,
  "title" TEXT, "body" TEXT, …        -- one column per CSV header, quoted
);

CREATE VIRTUAL TABLE entries_fts USING fts5(
  "title", "body", …,                  -- every source column except "id"
  content='entries', content_rowid='rowid',
  tokenize='porter unicode61'
);

CREATE TABLE tags (
  session TEXT NOT NULL,
  entry   INTEGER NOT NULL REFERENCES entries(rowid),
  field   TEXT NOT NULL,
  value   TEXT NOT NULL,               -- JSON
  PRIMARY KEY (session, entry, field)
);
CREATE INDEX tags_by_field ON tags(session, field);

CREATE VIRTUAL TABLE tags_fts USING fts5(
  value, session UNINDEXED, field UNINDEXED, entry UNINDEXED,
  tokenize='porter unicode61'
);

CREATE TABLE vectors (
  model TEXT NOT NULL, text_hash TEXT NOT NULL, vec BLOB NOT NULL,   -- float32, little-endian
  PRIMARY KEY (model, text_hash)
);

CREATE TABLE chunks (
  session TEXT NOT NULL,               -- '' for source fields
  field TEXT NOT NULL, entry INTEGER NOT NULL, n INTEGER NOT NULL,
  text_hash TEXT NOT NULL,
  PRIMARY KEY (session, field, entry, n)
);

CREATE TABLE applied (
  session TEXT PRIMARY KEY, log_digest TEXT NOT NULL
);
```

`entries` is written once per import and never updated; the kernel's
connection carries an authorizer that denies writes to it and to
`entries_fts`. `entries_fts` is built once with the `rebuild` command; the
source never changes, so it never drifts.

Tag values are JSON text: `"parking"`, `3`, `true`, `["a","b"]`. Reading a
tag decodes it. `tags_fts` mirrors `tags` row for row so `.lexical()` on a
tag field is a filtered `MATCH`.

`vectors` is content-addressed and dataset-wide: the same text embedded
under the same model is stored once, whichever session or field it came
from. A hash-keyed cache leaks nothing across sessions except that some
text was embedded. `chunks` maps a cell to the hashes of its passages;
source rows use session `''`, tag rows use the session name.

### Synchronizing index and log

The log is the truth; `tags` is a cache of it. Two rules keep them aligned:

- **On open.** The kernel computes a digest over the session's log files.
  If it differs from `applied.log_digest`, it deletes the session's rows
  from `tags`, `tags_fts`, and `chunks`, replays the log, and records the
  new digest. This is how a `git pull` takes effect.
- **On commit.** The log line is written and fsynced first, then the SQLite
  transaction commits, then `applied.log_digest` is updated. If the process
  dies between the two, the next open sees a digest mismatch and replays.

Nothing else writes `tags`. There is no export step and nothing to forget.

## Compiling the language

Every verb is one `SELECT` (or one `INSERT`) over `entries` aliased `e`,
with a `LEFT JOIN` per referenced tag field, per lexical query, and per
semantic query. The compiler walks the expression tree, emits SQL for what
SQLite does well, and calls registered deterministic Python functions
(prefixed `q_`) for the rest.

### Joins

| Reference | Join |
| --- | --- |
| tag field `f` | `LEFT JOIN (SELECT entry, value FROM tags WHERE session = :s AND field = 'f') t_f ON t_f.entry = e.rowid` |
| `.lexical(q)` on source `f` | `LEFT JOIN (SELECT rowid, -bm25(entries_fts) AS s FROM entries_fts WHERE entries_fts MATCH :q_k) l_k ON l_k.rowid = e.rowid`, with `:q_k` = `"f" : <sanitized terms>` |
| `.lexical(q)` on tag `f` | the same over `tags_fts` with `session` and `field` filters |
| `.semantic(q)` | `LEFT JOIN temp.sem_k ON sem_k.rowid = e.rowid`, a temp table the kernel fills before the query |

### Expressions

`x` is the SQL for the input; `j` is the raw JSON of a tag cell.

| Expression | SQL |
| --- | --- |
| `Field(f)` source | `e."f"` |
| `Field(f)` tag | `json_extract(t_f.value, '$')` for scalar use; `t_f.value` when a `q_` function needs the JSON |
| `.text()` | text: `x`; number: `CAST(x AS TEXT)`; any/list: `q_text(j)` |
| `.number()` | `q_number(x)`, returning `REAL` or `NULL` |
| `.length()` | text: `length(x)`; any/list: `q_length(j)` |
| `.lower()` `.upper()` `.strip()` | `lower(x)` `upper(x)` `trim(x)`, after `q_text` for any |
| `.search` `.findall` `.sub` `.slice` | `q_search(x, p, fl)` `q_findall(x, p, fl)` `q_sub(x, p, r, fl)` `q_slice(x, a, b)`; RE2 patterns compiled once per query |
| `.lexical(q)` | `CASE WHEN x IS NULL THEN NULL ELSE COALESCE(l_k.s, 0.0) END` |
| `.semantic(q)` | `sem_k.s` (`NULL` where the cell is absent) |
| `.isin([...])` | `x IN (…)`, with the numeric rule applied per literal |
| `.contains(v)` | text: `instr(x, :v) > 0`; list: `EXISTS (SELECT 1 FROM json_each(j) WHERE value = :v)`; any: `q_contains(j, :v)` |
| `+ - *` | SQL arithmetic; `NULL` propagates |
| `/` | `CAST(a AS REAL) / b`, so division is never integer division; `/ 0` is `NULL` |
| unary `-` | `-(x)` |
| `Random(seed)` | `q_random(:seed, e.rowid)`; `random()` when the seed is `None` |

### Predicates

SQL has three truth values; the language has two. Every comparison compiles
wrapped as `(x op y) IS TRUE`, so an absent cell compares false and `~`
over it is true, matching Python. `== None` and `!= None` compile to
`x IS NULL` and `x IS NOT NULL`. `& | ~` compile to `AND OR NOT`.

When a `text` or `any` expression is compared to a numeric literal, the
left side compiles as `q_number(x)`. When two expressions are compared,
no coercion is applied.

### Verbs

| Verb | Shape |
| --- | --- |
| `count(where)` | `SELECT count(*) FROM entries e <joins> WHERE <pred>` |
| `count(where, by)` | `SELECT <by_1>, <by_2>, … FROM entries e <joins> WHERE <pred>` fetched into Python, list cells flattened, counted with `Counter`, most common first |
| `values(expr, where, rank, limit)` | `SELECT <expr> FROM entries e <joins> WHERE <pred> ORDER BY <rank> DESC NULLS LAST, e.rowid LIMIT :n` |
| `retrieve(where, rank, limit, offset)` | `SELECT e.rowid, <rank> … ORDER BY <rank> DESC NULLS LAST, e.rowid LIMIT :n OFFSET :o`, then one `SELECT` for the cells of those rows and one for their tags |
| `tag(pred, f, literal)` | `SELECT e.rowid, e."id" … WHERE <pred>`, then `INSERT OR REPLACE INTO tags` per row; `None` deletes |
| `tag(pred, f, expr)` | `SELECT e.rowid, e."id", <expr> … WHERE <pred>`, then the same writes with the computed value; a computed `NULL` deletes |
| `entry[expr]` | `SELECT <expr> FROM entries e <joins> WHERE e.rowid = :r` |

`tag` also updates `tags_fts` and, for the touched rows, deletes their
`chunks` so the next `.semantic()` re-chunks them. It records
`{entry_id: value}` for the cell's log line as it goes.

### Registered functions

All deterministic, all `NULL`-preserving.

| Function | Behavior |
| --- | --- |
| `q_number(x)` | `int`, `float`, or numeric text → `REAL`; `true`/`false` → `1.0`/`0.0`; anything else → `NULL` |
| `q_text(j)` | JSON scalar → its text; array → items joined with `"\n"` |
| `q_length(j)` | text → characters; array → items |
| `q_search(x, p, fl)` `q_findall(x, p, fl)` `q_sub(x, p, r, fl)` | RE2 with `I`, `M`, `S` flags; `q_findall` returns a JSON array |
| `q_slice(x, a, b)` | Python slice on text or JSON array |
| `q_contains(j, v)` | substring on text, membership on arrays |
| `q_random(seed, rowid)` | a stable pseudo-random `REAL` from `(seed, rowid)` |

## Lexical

Queries are sanitized into FTS5 syntax, never passed through: each
whitespace-separated word becomes a quoted token, and a double-quoted span
becomes one phrase token. Words are joined with `OR`. The result is
restricted to the field's column (`"body" : …`). FTS5's `bm25()` returns
smaller-is-better negatives; the join negates it. Non-matching rows are
absent from the join and read as `0`; absent cells read as `NULL` by the
`CASE` in the expression table.

## Semantic

Long cells are split into passages on paragraph boundaries at roughly
1,500 characters. Every passage is hashed; hashes missing from `vectors`
are embedded in batches through the host (the kernel has no network; see
[`kernel.md`](kernel.md)) and stored. Queries are hashed and cached the same
way, so a repeated query costs nothing.

Scoring loads the field's passage vectors into memory once per kernel (a
matrix over `chunks` rows), computes cosine against the query, keeps the
maximum per entry, and writes `(rowid, score)` into `temp.sem_k`. With
numpy this is milliseconds for tens of thousands of entries; without it,
`math.sumprod` over `array('f')` rows is a few times slower and still
acceptable. Past roughly 100k entries exact scoring stops being cheap, and
approximate indexes are out of scope for core.

A tag field's passages are keyed by session; a `tag` write drops the
touched rows' `chunks` so they are re-chunked on the next query.

## Export

`quail_export` and `quail export` write `exports/<session>.csv`: `id`, the
source columns, then one column per tag field in this session, with
non-scalar values JSON-encoded. The log is already the methods record and
is not duplicated.

## What hosted attaches to

Hosted needs three things from this layer and nothing else: a `Project`
that resolves a directory into manifest, datasets, and sessions; an index
builder that turns a CSV into `.quail/<dataset>.quail`; and the log format,
which it may read to show history or write to import work done elsewhere.
Whether hosted keeps projects in git, in per-user directories, or both is
its decision. Core never contacts a remote.
