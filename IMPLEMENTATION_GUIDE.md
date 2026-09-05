# Implementation guide

This is the implementation contract for the Quail core rebuild. Its goal is
a small environment in which an agent can inspect a corpus, write annotations,
and continue that work on another machine.

This revision deliberately changes only this guide. The documents under
`docs/` and the repository orientation files still describe parts of the
earlier design. Where this guide specifies different behavior, follow this
guide for the rebuild. Section 10 lists the changes to move into their owning
documents later. This is a temporary documentation transition: the shipped
agent document must agree with the implemented language.

The guide fixes observable behavior and ownership. It does not prescribe
the order of function definitions, a class for every concept, or a final
file count. Use ordinary modules and small records. Add machinery only when
a concrete behavior needs it. Simplicity comes from shared paths and clear
ownership; it is not permission to remove supported workflows.

## 1. The design

An external agent drives one foreground CLI host. That host owns one Python
kernel for the lifetime of an execution stream. The kernel evaluates the
analysis language; the host owns durable files and embedding requests.

Five rules carry the design:

1. **Each open kernel sees one source snapshot.** Stable entry IDs let a
   session continue across source edits. Generated positional IDs belong
   only to their source version and cannot silently move annotations.
2. **Tags are durable analysis state.** Python variables are working memory.
   A normal failed cell rolls back its tags and keeps its earlier assignments
   and captured output. Restarting the kernel discards working memory.
3. **The log decides what committed.** A host acknowledges a cell only after
   its complete result and tag delta are synced to the session log. SQLite
   caches that history; it does not compete with it as a source of truth.
4. **A cell writes only private working tables.** Arbitrary Python and
   embedding waits never hold a writer transaction on the shared index.
5. **Caches do not define answers.** Rebuilding SQLite, warming first, or
   working in another session must not change the meaning of a query.

Core provides the CLI, language, project format, and local runtime. Hosted
may wrap these with authentication, MCP, and container placement. Core does
not run an agent, call a language model, run Git, or manage remote workers.

The first implementation targets Python 3.12+ on Linux and macOS. Use the
standard library, SQLite with FTS5, and `google-re2`. NumPy is an optional
scoring accelerator. There is no Core MCP dependency.

### Ownership

Start with this layout; each row is a responsibility, not a framework.

| Module | Owns | Dependencies within Core |
| --- | --- | --- |
| `project.py` | Project configuration, paths, metadata, locks, run-log writing and parsing, replay | None |
| `index.py` | CSV import, source indexes, materialized tags, vector storage, warm-pack validation and ingestion, cache synchronization | `project.py` |
| `embed.py` | The two embedding HTTP dialects, timeouts, retries, response validation | `project.py` |
| `prelude.py` | Expression nodes, SQL compiler, verbs, private tag tables, scoring, cell execution, confinement | None |
| `kernel.py` | Child lifetime, control exchange, limits, durable cell completion, cached embedding requests | `project.py`, `index.py`, `embed.py` |
| `service.py` | Project operations, the shared dataset-open path, session opening, export, local and shared warming | Host modules above |
| `cli.py` | Argument parsing, the foreground stream, presentation, exit status | `service.py` and returned kernels |

Keep `quail/__init__.py` inert. The child starts with
`[sys.executable, "-m", "quail.prelude"]`; importing the package must not
load the host graph.

`prelude.py` is self-contained so that Hosted can place it in a confined
process. It does not parse manifests or logs, perform provider HTTP, or
write durable files. Replay has one implementation, in `project.py`.
The host initializes the child's working tags from the synchronized index.

SQL belongs in `index.py` for host operations and `prelude.py` for
language execution. Transport code calls those operations; it does not
reimplement their queries. A few small wire/error records may exist on both
sides of the process boundary and must share contract fixtures.

## 2. Projects and source versions

The durable project remains ordinary text:

- `quail.toml`: project, dataset, provider, and limit configuration.
- Dataset CSVs at their registered paths.
- `sessions/<name>/session.toml` and `sessions/<name>/log/*.jsonl`.
- Optional CSV exports beneath `exports/`.

Optional `warm/` packs carry derived embedding vectors between machines.
They may travel with the project through Git, but are not analysis truth;
missing packs never prevent opening or analyzing the source.

`.quail/` holds disposable SQLite files and local locks and is gitignored.
Remove it only when Quail processes using the project are closed. Core never
commits, pulls, or pushes anything.

Dataset and session names are non-empty single path segments; reject `.`,
`..`, separators, and NUL instead of rewriting them. Resolve project paths
against the manifest root and keep managed paths inside it. Source CSVs
cannot occupy the manifest, ignore file, or the managed `.quail/`,
`sessions/`, and `warm/` directories. Validate resolved paths before
creating files.

### Manifest and commands that create files

Keep the manifest shape and kernel defaults from `docs/storage.md`, with
one addition: an embedding configuration includes an explicit revision.

```toml
[project]
quail = "1"

[datasets.notes]
source = "data/notes.csv"
id = "id"                         # optional; resolution is described below
embed = "ollama/embeddinggemma"   # optional; selects dialect and model
embed_revision = "study-model-v1"

[providers.ollama]
base_url = "http://127.0.0.1:11434"
```

An embedding revision is a non-empty operator designation for fixed model
weights and embedding behavior. It is required when `embed` is configured;
it is not inferred from a mutable model name. Section 6 defines its use.
Without `embed`, lexical analysis works and semantic search gives a
configuration error.

Unknown keys fail clearly. Provider credentials are always `env:NAME`
references, resolved by the host at request time. Never put literal secrets
in the manifest, logs, or child environment.

`quail init [DIR]` creates the target when needed, refuses an existing
manifest, writes only the minimal `[project]` table, ensures `sessions/`
exists, and adds `.quail/` to the existing ignore file without replacing
unrelated content. Lock directories may be created as needed; init creates
no dataset, example data, or kernel.

`quail import CSV` registers a new dataset and builds its index. Resolve
the CSV from the invoking working directory and require its resolved path
to remain inside the project. Its default dataset name is the file stem.
Do not copy or rewrite the CSV. Refuse an existing dataset name.

Validate the complete CSV and prospective configuration before atomically
appending one safely quoted dataset table to the manifest. Preserve other
tables and comments. If indexing subsequently fails, the valid registration
remains and the next open can build its cache. Use `tomllib`; no TOML writer
dependency is needed for this one append operation.

### CSV and identity

Read UTF-8 CSV with a header; accept a UTF-8 BOM. Preserve cell text exactly,
including leading whitespace and numeric-looking strings. An empty cell is
`None`. Do not infer types.

Expose one canonical source field named `id`:

- An explicitly selected ID column supplies it and is renamed to `id` in
  the public schema, without a duplicate field.
- Otherwise an exact `id` header supplies it.
- Otherwise synthesize `row-000001`, `row-000002`, and so on in file order.
  Report that these IDs are meaningful only within this source version.

IDs must be non-empty and unique. A different selected ID column together
with an existing `id` column is ambiguous and is rejected. Validate header
emptiness, NUL, row width, duplicate public names under SQLite's identifier
comparison, the internal `rowid` name, and the active SQLite column limit.
Quote unusual valid names rather than rewriting them. Report source row or
header locations for import failures.

Define `source_hash` as SHA-256 of the exact CSV bytes, prefixed `sha256:`.
Define `source_version` as the same hash of canonical JSON containing:

```json
{"import_format":1,"source_hash":"sha256:...","id_column":"id"}
```

`id_column` is the resolved original column name, or JSON null for generated
IDs. Canonical JSON means UTF-8, sorted object keys, compact separators,
unescaped Unicode, and no non-finite numbers. The descriptor uses effective
import behavior, so explicitly selecting the already-selected `id` column
changes nothing. Changing source bytes or the selected ID column creates
a new version.

Hash and import the same byte stream. Never record a hash of one read and
publish rows from a different read. Detect a source edit during import and
leave the previous index intact.

### Sessions and source edits

On creation, store `dataset`, the initial `source_version`, `created`, and
the initial resolved `id_column` in `session.toml`. Omit `id_column` for
generated IDs. `forked_from` and `description` remain optional. The dataset
is immutable session scope; each run records the actual ID column and source
version it analyzed.

When the source supplies stable IDs, a changed CSV automatically rebuilds
the index on the next open. Continue the same session and reapply its tags
by canonical ID. New entries start untagged. Final non-null `(entry, field)`
values whose IDs are absent from the current source remain in the logs and
count as orphan tags; a final clear does not count. If an ID returns, its
tags become visible again. Sessions and exports report the orphan count.
Renaming or changing the supplying column does not itself require a new
session: continuity follows the canonical ID values. Core does not infer
a correspondence between different ID strings.

Supplied IDs declare persistent entry identity; do not recycle an ID for an
unrelated entry. Preserving tags follows that declaration. It does not
claim that an annotation is still appropriate after the entry's text changes.
Report source-version changes when opening a session, so the analyst can
review affected work. Keep provenance in the run headers; do not introduce
an automatic annotation-revalidation or migration framework.

Generated `row-...` IDs cannot establish continuity after arbitrary edits
or reordering. While a dataset still uses generated IDs, an existing session
can open/export only against its initial generated-ID source version. If it
differs, require restoring that source or supplying explicit stable IDs.
Materialize the original canonical IDs into the CSV before editing/reordering
it; selecting that column then declares their continued identity. Merely
numbering the already-reordered rows is not continuity. New sessions remain
available when continuity is not intended. Listing identifies unavailable
sessions without showing empty tags as if their work had disappeared.

An open kernel always keeps its source snapshot. Close affected streams
before rebuilding changed data. If a new source field collides with a
session's recovered tag field, do not materialize or open that session until
the name conflict is resolved. Report it without blocking unaffected sessions
or hiding either field.

An existing session rejects `fork_from`; a supplied dataset must match.
A new session uses its requested dataset or the project's sole dataset.
When forking, the source session supplies its dataset and identity provenance;
reject any conflicting dataset argument. Ambiguity is an error. Setup lists
sessions and never allocates one.

Fork by copying a closed source session's logs into a new destination and
writing its metadata. Preserve run IDs and source/ID provenance. Hold the
source session lock during the copy; publish the destination only after
the copy succeeds.
Never overwrite a destination or share writable files through hard links.
Forking a historical session is allowed. Forks retain the same rules for
stable-ID continuation and generated-ID source compatibility.

## 3. Logs, replay, and durable completion

### One host-owned log per kernel run

The host alone appends `sessions/<name>/log/<run-id>.jsonl`. Use a UTC
timestamp plus a UUID for the run ID, create the file exclusively, and
never reopen an old run for appending. Reset or child replacement starts
a new run. Times describe history; they do not order writes.

The first complete line is a versioned header containing the run ID,
start time, actor, Quail version, dataset, ID column, source version, source
hash, resolved embedding identity if configured, and actual confinement mode.

```json
{"format":1,"run":"...","started":"...","actor":"...","quail":"...","dataset":"notes","id_column":"id","source_version":"sha256:...","source_hash":"sha256:...","embedding":null,"confinement":"audit"}
```

`format` versions the log schema separately from the Quail release. When
configured, `embedding` records `{id, embed, revision}`. Confinement reports
`audit` or `audit+netns` according to the protection actually established.
`actor` comes from `QUAIL_ACTOR` or the hostname and is provenance only.
The run ID must match the filename. The header does not bind the run to a
session name, so a fork can retain it. `id_column` is null for generated IDs.
Headers agree with the session dataset. Stable-ID runs may name different
ID columns and source versions; generated-ID runs must name the initial
generated-ID version. Validate the source-version descriptor against the
header's source hash and resolved ID column.

Every following complete line records exactly one submitted cell:

| Field | Meaning |
| --- | --- |
| `n` | Contiguous cell number within the run, starting at 1 |
| `order` | Positive logical order, strictly increasing within a run |
| `started`, `ended` | UTC timestamps for display |
| `code` | The exact submitted text |
| `output`, `truncated` | The bounded output and truncation flag |
| `error` | Null or `{type, message, hint}` |
| `tags_written` | Number of distinct entry/field pairs in the final delta |
| `tags` | Per-field maps of canonical entry IDs to final JSON values; null clears |

The host assigns `n`, `order`, scope, and submitted code. Validate the
child's result before logging: expected request number, output shape,
valid field names, source-field protection, IDs in this source version,
and JSON values. An error result has an empty delta and zero writes.

Collapse multiple writes to one entry/field within a cell to the final
value, including clears. An unchanged value explicitly written by the
cell may remain in its delta. `tags_written` counts these distinct writes;
it is not the sum of all `tag()` return values.

### Logical order and merging

On session open, replay its available history and remember the largest
`order` across all accepted complete cell records, including failures.
The next cell uses that number plus one; each recorded cell advances it again.

Replay successful records in ascending `(order, run_id, n)` order. The last
write to an entry/field wins and null clears it. This is a small logical
clock: a run continuing observed history always writes after that history,
even when the machine's wall clock moves backward. No timestamp comparison,
vector clock, or distributed lock is needed.

Concurrent runs on separate machines may choose equal logical orders;
the run ID breaks ties deterministically. This is a defined merge policy,
not a claim that concurrent coding decisions agree. All original writes
remain in the logs. Use separate named sessions or forks for independent
coding that must remain independently inspectable. Git merges their files;
it does not choose which analyst is right.

A live stream uses the history it synchronized when opened plus its own
new records. Pulled or manually edited history is incorporated on the next
open, not halfway through a cell. Cache digests must describe only history
actually applied, never newly discovered records the kernel has not seen.

### Parsing and cache markers

Validate headers, record schema, scope, numbering, logical orders, and tag
values before applying history. Preserve best-effort recovery: ignore a
malformed complete cell as a whole and report its file, line, and reason.
Never apply a partial tag delta. A malformed, unsupported, or wrong-scope
header excludes that run and reports why. Conflicting duplicate cell
identities are excluded rather than arbitrarily choosing a copy. Writers
produce contiguous numbers; recovery may retain later valid cells after an
ignored gap. Accepted numbers and logical orders still increase within a run.

Keep every original file untouched. Include a structured recovery summary
in setup/session information and opening results, and show human diagnostics
on stderr. The summary identifies excluded records/runs and whether history
is incomplete. Cached opens must retain these diagnostics. If completed log
content contains no usable run, fail clearly instead of presenting a recovered
empty analysis. Wholly interrupted run creations are empty history and do not
strand a new session. Historical listings can parse logs without a source.

The index maps recovered writes to the current stable IDs and reports final
orphans separately from malformed history. Missing current IDs are not a
parse failure. New child results are still validated against their live
source before logging; recovery is not permission to write invalid records.

A final fragment without a newline is an interrupted append. Report and
ignore that fragment; do not modify the old run. Valid complete records
before it remain usable. An empty file or wholly unterminated header is
likewise an interrupted run creation with no cells to apply. A complete
invalid header is reported and excluded as described above.

The session digest is SHA-256 of canonical JSON containing sorted
`[log_filename, sha256_of_exact_file_bytes]` pairs. Include failed records,
excluded records/runs, and ignored trailing fragments in the digest. The
digest is a cache marker, not an ordering or identity system. It is independent
of the session directory name, so copied history has the same digest.

Cache synchronization replays once when the digest or source version
differs from the stored `applied` marker, then replaces that session's
materialized tags, orphan count, and marker together. At runtime the host may
update its current file hash incrementally. Do not re-read all past cell code
after every successful cell.

### A cell has one durable commit point

1. The host sends the next numbered cell to its child.
2. The child begins a transaction on its private tag tables, runs the
   cell, and finalizes bounded output.
3. On normal success it commits those private working tables and returns
   the result and tag delta. On an exception it rolls back those tables,
   clears the delta, and returns the error and captured output. Python
   assignments made before the exception remain.
4. The host validates the result, writes one complete JSON line, flushes,
   and fsyncs the run log. This is the durable commit point.
5. In a short independent SQLite transaction, the host applies the delta
   and new digest to the materialized cache. No Python or HTTP runs inside
   that transaction.
6. The host returns the result. It sends no next cell before completing
   these steps or discarding the child after a host failure.

The child's early private commit is invisible to other processes. If the
host cannot durably finish the cell, that child must not continue with its
unacknowledged working tags. This needs no prepare/commit RPC: the host
already serializes requests and can terminate its own child.

Use the same log-and-cache completion path for successful and failed
cells. Sync newly created run files and their containing directory before
acknowledging their records. Atomic metadata replacement also syncs the
file and directory.

| Failure point | Required outcome |
| --- | --- |
| Cell raises normally | Log the failure with no writes; keep the child and its earlier Python assignments |
| Child dies before a complete valid result | No tag delta can commit; log a kernel-failure result, replace the child from committed state |
| Child dies after a complete result arrives | The host can durably finish that result, then replace the child |
| Log write or fsync has an uncertain outcome | Stop the stream, discard the child, and report a persistence failure with the run/cell identity; never append a contradictory failure record for that cell |
| Cache update fails after a synced record | The record remains committed; retry or reconstruct only the cache, never execute the code again or turn that committed cell into a rolled-back result |
| Host dies before replying | Reopening recovers complete records; an unacknowledged cell may have committed, so do not automatically resubmit it |

After a log I/O failure, a later opener uses complete records actually
present on disk as recovery truth and syncs recovered files before
acknowledging a usable session. Do not pretend an uncertain append is known
to have rolled back. If cache recovery still fails, close with a host error
that identifies the already-committed cell and log path. Keep the original
result recoverable in its record.

A hard process death can lose output still buffered inside the child.
Preserve output for normal exceptions; do not promise bytes that never
reached the host. Report child replacement with `kernel_restarted`.
Never implicitly rerun submitted Python.

## 4. SQLite, opening, and concurrency

Keep one WAL-mode SQLite file per dataset at
`.quail/<dataset>.quail`. The host's operations use short transactions
and a finite busy timeout. Build replacements at a temporary path and
publish only after validation and clean closure of the temporary database.
Checkpoint and close a replacement's WAL before publishing its main file.

The shared index contains:

| Data | Representation and lifetime |
| --- | --- |
| Import metadata | Schema version, source hash/version, canonical ID resolution, ordered source fields |
| Source rows | `entries`, with import-order integer rowid and unique text `id` |
| Source lexical indexes | One single-column FTS5 table per non-ID source field |
| Materialized session tags | `tags(session, entry, field, value)`, keyed by session, stable entry ID, and field; value is canonical JSON |
| Applied history | `applied(session, source_version, log_digest, orphan_tags)` for the current materialization |
| Embedding vectors | `vectors(embedding_id, text_hash, vec)`, keyed by identity and exact text hash |

Use foreign keys for tag IDs, an index on `(session, field)`, and
parameter binding for values. Generated FTS table names derive from field
names through a collision-resistant hash; CSV names never become
unquoted SQL. Tag caches exist only for sessions compatible with the indexed
source: supplied stable IDs preserve identity across versions, while
automatic positional identity requires the initial generated-ID version.

There is no shared `tags_fts`, passage table, or persistent
`semantic_values` table. Per-entry semantic mappings are cheap to derive
from immutable source text or a kernel's private tags. Keep them in that
kernel and invalidate them when needed.

### The child's connection

During bootstrap, the child opens the synchronized index in SQLite
read-only mode and copies its session's tags into indexed TEMP tables.
Use disk-backed TEMP storage (`temp_store=FILE`) with a bounded page cache;
do not require the entire tag set to fit in Python or SQLite memory. The
host supplies a private scratch directory beneath `.quail/` through SQLite's
`SQLITE_TMPDIR` environment variable before startup. SQLite manages its
temporary files; the host removes the directory after the child exits.
Verify that the SQLite build permits file-backed TEMP storage, following
[SQLite's temporary-file rules](https://www.sqlite.org/tempfiles.html).
Abandoned scratch is disposable when its owning processes are closed.

Build tag FTS tables lazily in TEMP, one per field when first searched.
Source reads explicitly use `main`; tag reads and writes explicitly use
`temp`. Join tags to
`entries.id` by canonical ID; lexical and semantic score tables use internal
rowids. Do not copy the old shared-tag join against `entries.rowid`.

A cell transaction changes only TEMP. It may hold a read snapshot on the
shared database, but it never upgrades that snapshot to a shared write
transaction. Host vector inserts or another session's tag commits can
therefore proceed while the child evaluates.

The child may read vectors already visible in its read snapshot. Missing
vectors are obtained from the host and used directly from its response:
do not expect a long-lived read snapshot to see the host's new inserts.
The host checks the shared cache again when servicing a request.

A tag write updates its private value and any existing field FTS index
in the same transaction. Invalidate cached semantic mappings, matrices,
and score tables for that field. A failed cell rolls back values and FTS,
restores the field catalog, and drops in-memory derived state touched by
the failed cell. Avoid a second journal for those disposable caches.

Read-only host operations use materialized committed tags. They never
inspect the child's TEMP tables, so export cannot see a half-finished cell.

### One opening path

`service.py` owns dataset opening for setup, sessions, fields, export,
warming, and kernel creation:

1. Discover and validate the manifest and selected dataset.
2. Check the index under a shared dataset lock.
3. If absent or stale, release shared access, take the dataset lock
   exclusively, recheck, and build a replacement.
4. Hold shared access for the operation, or for the lifetime of an opened
   kernel, and ingest compatible shared warm packs before returning it.

An operation that will publish project or session metadata acquires the
project metadata lock before entering this path, preserving the lock order
below. Existing-session execution needs no metadata publication lock.

Rebuild source rows and FTS, preserve compatible vector rows when available,
and replay compatible session histories by ID, recording orphan counts.
Unavailable generated-ID sessions remain on disk and visible in listings. A schema
mismatch rebuilds a disposable cache; it does not trigger user-data migrations.

Opening a session then takes its session lock, checks its ID/source scope,
synchronizes its log, and spawns the child. Validate configuration before
creating new session metadata. Kernel reset retains its locks, index,
source version, and resolved embedding configuration; reload configuration
by closing and reopening the stream.

Fields/export acquire and synchronize a closed session before reading.
If its session lock is already held, read one committed WAL snapshot of
its cache without replay. Require an `applied` marker for the matching
source version; an owner still initializing an uncached session means
temporarily unavailable, not an empty analysis. This may show the previous
committed cell while the owner finishes publishing the next one. A response
already acknowledged by its owner must be visible to a subsequent read.
If the current source changed, require the affected streams to close before
rebuilding.

### Locks are local lifetime protection

Use ordinary advisory `flock` files beneath `.quail/locks/` for project
metadata, datasets, and sessions. A live kernel host holds its dataset lock
shared and its session lock exclusive. Reset retains them. Rebuild requires
exclusive dataset access and fails clearly if a live kernel prevents it.

Serialize init/import/session creation and fork publication with the project
metadata lock. When multiple lock classes are needed, acquire project,
dataset, then session; release the metadata lock once publication is done.
Use nonblocking acquisition for conflicts with live sessions and rebuilds.
Do not add a lock server or a cross-machine lease.

No host operation holds a shared-index writer transaction across a cell,
provider call, or user interaction. Retry bounded cache transactions when
safe; never retry arbitrary Python. Multiple hosts may request the same
embedding concurrently; duplicate computation is acceptable, duplicate
cache keys are not.

## 5. The analysis language

Keep the compact shape in `docs/api.md`: `Field`, `Random`, ordinary
numeric expressions, predicates, `count`, `retrieve`, `values`, `tag`,
and `fields`. There is one expression-to-SQL engine in `prelude.py`.
Python UDFs implement individual operations SQLite cannot faithfully
provide; they are not a second row-by-row execution engine.

Compile expressions to parameterized SQL and reuse joins within a query.
Carry the value's kind and encoding with its compiled fragment: source
text, SQL numbers, and JSON-encoded tag/list values are distinct. Decode
JSON at explicit boundaries, never by guessing from string contents or by
discarding its type through `json_extract` before a typed comparison.

Expression construction inspects the cached field catalog and type
information, but reads no rows and performs no search or embedding.
Reject unknown fields and invalid method/produce pairs at construction.
An expression referring to a tag field must also be checked when evaluated,
since rollback or clearing its last value can remove that field.

Keep one method-signature table for construction checks. Methods that return
predicates are actually `Predicate` objects. Expressions and predicates
reject Python truth testing and iteration; `&`, `|`, and `~` compose
predicates. Reject direct `is`/`is not` syntax before a cell executes
because Python identity cannot be overloaded for symbolic values. This is
a language guard, not a security mechanism.

### Values, absence, and comparisons

Source values are text or None. Tags accept JSON scalars, arrays, and
objects with string keys and finite numbers, including nested nulls.
Top-level None means absence and is represented by no tag row.

Use these runtime rules for an `any` tag value:

| Operation | Behavior |
| --- | --- |
| Text conversion | Strings unchanged; other scalars use JSON spelling; objects use canonical JSON; arrays join recursively rendered items with newlines, spelling nested nulls as `null` |
| Length | Characters of strings, elements of arrays, keys of objects; None for other scalars |
| Slice | Python slicing for strings/arrays; None for other kinds |
| Contains | Substring for strings, Python item membership for arrays, key membership for objects; false for other scalars |
| Substitution | Apply to each array item through text conversion, preserving null items; otherwise substitute in the converted text |
| Case/strip/regex/search | Use the same text conversion before the operation |

Value-producing operations propagate top-level None. Predicate-producing
operations return booleans, never SQL NULL. Comparisons involving an absent
operand are false except explicit comparisons to a None literal:
`== None` tests absence and `!= None` tests presence. Negation therefore
includes absent rows when negating an ordinary false comparison.

Numeric literals trigger the documented numeric conversion of the other
operand. Numeric conversion accepts finite numeric text and numbers,
including bool as 0/1; failure returns None. Two expressions do not gain
implicit numeric conversion. For JSON scalar comparisons preserve Python
scalar equality; ordering incompatible scalar types returns false.
Container equality compares decoded JSON values, and container ordering
returns false. Do not inherit SQLite's arbitrary text-versus-number
ordering or compare a JSON-encoded object as if it were a source string.

`.isin([...])` is equivalent to OR-ing comparisons with each supplied
scalar literal, including None. An empty list is false. This keeps mixed
literal lists consistent with individual comparisons. Use `.contains()`
for membership in list-valued cells. Reject non-scalar `.isin` literals.

Use Python Unicode `lower`, `upper`, `strip`, and `len` through UDFs
where SQLite differs. Numeric arithmetic returns finite numbers or None;
absence propagates and division by zero is None. Regex patterns use RE2,
with only the documented `re.I`, `re.M`, and `re.S` flags.

### Verbs and entries

- `where` is None or a Predicate; `rank` is a number expression.
- `retrieve` defaults to 10. Limit and offset are nonnegative integers
  excluding bool. Clamp only retrieve's limit to `max_limit` and report it.
- `values` retains None and accepts an uncapped nonnegative limit or None.
  It remains subject to the kernel memory limit. Statistics examples must
  filter absence explicitly.
- Rank descending with None last, then import order as a stable tie-breaker.
  Negation gives ascending numeric rank while still placing None last.
- `Random` accepts the ordinary `random.Random` seed types; numeric seeds
  must be finite. Use a private seeded generator to choose the expression's
  integer salt once, with None choosing it afresh. Hash that salt and the
  canonical entry ID with a stable algorithm to produce numbers in `[0, 1)`.
  Reusing an expression keeps its per-entry numbers. Neither SQLite rowids
  nor Python's randomized string hash participates.

`count(by=...)` returns a Counter. A scalar or None contributes one key;
a flat list contributes one per item, including repetitions. Multiple
grouping expressions take the Cartesian product of those contributions.
An empty list contributes nothing, so totals may exceed or fall below the
entry count. Follow ordinary Counter scalar equality and use import order
to break frequency ties. Objects and containers remaining after that
one-level expansion are supported grouping values. Represent their keys as
the plain tuple `("json", canonical_json)`: for example, `{"a": 1}` becomes
`("json", '{"a":1}')`. A literal string keeps its string key, so the two
cannot collide. Cross-tabs contain these keys inside their outer tuple.
This needs no custom key class or separate grouping engine.

`tag(None, field, value)` targets all entries. Other targets are a
Predicate, an Entry, or a list of Entries. Deduplicate entry lists by ID
and validate their dataset version and kernel scope before writing.
The return value is the distinct target count, including targets whose
value was already equal.

A tag field is a non-empty string without NUL that does not name a source
field. Its first non-None write creates it and clearing its last value
removes it. Each `tag` call resolves its complete target and computed values
before applying its writes, so its predicate cannot change partway through
that same call. Later lines in the cell see those writes. Snapshot JSON
values at the write; later Python mutation must not alter an already-staged
tag. There is one replace/clear operation, with no append or separate untag API.

Entries are read-only handles to rows in this kernel and source version.
String lookups and expression lookups read the current working state;
both see earlier tag writes. The mapping covers the current field catalog,
with None for an absent cell and KeyError for an unknown string key.
`entry.id` is canonical ID. `entry.score` records its retrieval rank
value; it does not silently change after a later tag write. Materialize
a dict when a caller wants a value snapshot. Source cells can be cached
because this source version is immutable.

Expose the documented verbs, constructors, error, pre-imported modules,
and `quail` recovery object. Use an explicit expected public namespace in
tests; example variable names and transport names are not public bindings.
Do not reserve ordinary Python assignment names.

## 6. Search and embeddings

Both searches produce numeric expressions, but start directly from a
stored source or tag Field. The canonical ID is not searchable.
A transformed search raises at construction with a hint to tag the
transformed value first. This gives both source and derived analysis
one indexing path. Query strings must contain non-whitespace text.

### Lexical corpus boundaries

Use FTS5 with `porter unicode61`, and one single-column corpus per field.
Source corpora are built at import. Tag corpora are private to a kernel
and built lazily from that session's working tags.

Index each present value as one document using its text conversion;
document rowids correspond to source rowids. Include present empty text
as an empty document; absent values have no document. Restrict BM25
statistics to this field and, for tags, this session. Another field's
contents or another session's work cannot change its scores.

Sanitize queries into quoted tokens and quoted phrases joined with OR.
Do not pass user FTS syntax through. There is no cross-column filter
expression to get wrong because each corpus has one text column.
Negate FTS5's BM25 score so higher is better. Absent values score None,
present nonmatches score zero, and matches score positively.

Compute corpus statistics over the full field, independent of a verb's
candidate filter. Scores are relative and not comparable across fields.
Updating private tags updates an existing private FTS index in the same
cell transaction. A rollback restores both.

### One complete value is the semantic unit

Embed each complete non-empty rendered value once; its score is cosine
similarity to the complete query vector. Empty rendered text has no semantic
vector and scores None, even when the underlying tag is present.

This first version is suited to rows that are meaningful analysis units,
such as one survey answer or one prepared excerpt. It provides no passage
splitting, best-passage aggregation, automatic truncation, ANN index, or
model-window estimator. Long transcripts should be prepared into suitable
rows outside Core when passage retrieval is needed.

Pass complete text to the provider and fail clearly on oversized input.
For Ollama `/api/embed`, explicitly send `truncate: false`; its default
otherwise truncates. Do not assume an HTTP success from an arbitrary
compatible endpoint proves that endpoint preserved all input. Supported
provider configurations must honor rejection instead of silent truncation.

### Embedding identity and one cache path

An embedding identity is SHA-256, prefixed `sha256:`, of canonical JSON
`{"format":1,"embed":<exact configured string>,"revision":<embed_revision>}`.
Split `embed` at its first slash: `ollama` or `openai` selects the wire
dialect and the remainder is the provider's model name.

Base URLs and credentials route requests and do not belong in identity.
The revision must change when weights or embedding behavior change,
including any externally configured preprocessing. Freeze the resolved
embedding configuration for each open stream and record the identity in
its run header. Core validates vectors; it does not attest remote weights.
A revision is an explicit reproducibility obligation, not evidence that
two arbitrary endpoints are equivalent.

Hash exact rendered UTF-8 text with SHA-256, prefixed `sha256:`. The host owns
one cached embedding operation used by both kernel requests and `quail warm`:

1. Deduplicate inputs, preserving the mapping back to request order.
2. Read existing `(embedding_id, text_hash)` vectors.
3. Call the provider for missing texts in bounded batches, outside every
   database transaction.
4. Validate and insert completed batches in short transactions.
5. Return the canonical stored vectors in input order.

Infer dimensions from an existing vector for that identity, or establish
them with its first inserted batch. Recheck inside the writer transaction,
so simultaneous first requests cannot establish different dimensions.
Validate response count, finite coordinates, nonzero dimension and norm,
and the little-endian float32 representation after packing. An existing
key wins a concurrent insertion; return that stored vector to both callers.

`embed.py` performs HTTP only. Use the standard-library client for Ollama
and OpenAI-compatible endpoints, preserve request order (including indexed
OpenAI response items), and use finite request timeouts with a small fixed
retry bound for transport, rate-limit, and server failures. Do not retry
authentication, invalid-input, dimension, or schema errors.

`index.py` owns cache reads, validation at insertion, and writes. A plain
host function in `kernel.py` composes it with `embed.py`; both kernel
execution and `service.py` warming call that function without requiring a
child. A cache batch may survive a failed cell: vectors are derived
operational state, not annotations.

The child prepares field-to-vector mappings and score tables in memory.
Use packed vectors or NumPy arrays rather than a full corpus of Python
float objects. Serve requests in batches; internal vector responses can
carry base64 packed float32 to avoid repeatedly expanding the corpus into
JSON numbers. Use returned vectors directly when the child's read snapshot
predates the cache insert. Source mappings persist within the kernel;
tag writes invalidate affected fields.

Cosine computation has a standard-library path and an optional NumPy path
with the same semantics. Test numeric agreement within a stated tolerance;
do not promise bitwise identity across numerical libraries or provider
recomputations. Identical source, tags, query, embedding revision, and
compatible provider behavior must give equivalent warm and cold results.

### Local and shared warming

Shared warming is part of the initial Core capability:

```text
quail warm DATASET [--field F] [--shard I/N] [--json]
```

Warm a selected non-ID source field, or all non-ID source fields. Reject
the canonical ID, tag fields, and unknown fields. Both forms start no
session, execute no synthetic cell, and write no analysis log. Tag fields
continue to warm inside their sessions. Lexical indexes already exist;
warming prepares semantic corpus vectors, not future query strings.

Without `--shard`, warm the full selected inventory into the local cache.
With `--shard I/N`, warm that deterministic fraction and publish its vectors
under `warm/` for transfer through Git. Both forms use the same host cached
embedding function as a semantic query. `index.py` owns inventory, pack
encoding/validation, and cache insertion; `service.py` coordinates the work.
Report selected, reused, and newly embedded value counts, plus any pack path.

### Shard assignment

Build the distinct non-empty text inventory from the selected source fields
using the same text conversion and hashing as semantic search. Sort it by
text hash. For `M` values and one-based `1 <= I <= N`, select indices:

```text
start = ((I - 1) * M) // N
stop  = (I * M) // N
start <= j < stop
```

Reject invalid shard syntax or bounds. The ranges are disjoint and cover
the inventory, with sizes differing by at most one. Assignment depends on
the value set, not row order, machine, cache hits, or provider address.
Compatible ranges can compose across shard counts: `1/4`, `2/4`, and `2/2`
cover the same inventory as one full warm.

Workers use the same source/import configuration, selected fields, and
embedding identity, including its explicit revision. Different base URLs
are fine under that declared identity. An empty range reports zero work
and writes no pack. It is not a missing completion record.

### One portable pack format

Use one JSONL format and deterministic part paths:

```text
warm/<dataset>/<source-version-hex>/<plan-hash-hex>/part-0001-of-0008.jsonl
```

The directory components use the bare hash hex digits. Compute `plan_hash`
from canonical JSON containing `format: 1`, `embedding_id`, the sorted
selected `fields`, and `shards: N`. Source version scopes the parent
directory. There is no separate plan file or worker manifest.

The first line is the complete header:

```json
{"quail_warm":1,"dataset":"notes","source_version":"sha256:...","source_hash":"sha256:...","embedding":{"id":"sha256:...","embed":"ollama/embeddinggemma","revision":"study-model-v1"},"fields":["body"],"dims":768,"shard":[1,8]}
```

Every following line contains one selected vector, sorted by text hash:

```json
{"text_hash":"sha256:...","vector":"<base64 little-endian float32>"}
```

Include every vector in the selected range, whether reused locally or newly
embedded. A machine with a warm cache must still produce a complete pack
for a cold recipient. Read canonical stored vectors after insertion so
concurrent cache fills and pack output agree. Source text, entry IDs,
per-entry mappings, and session tags do not travel in a pack.

Write a temporary file beside its destination, finish and sync it, then
publish atomically. Never append to a published pack. An interrupted warm
may leave useful local vectors but no partial final pack. Re-running the
same shard may atomically replace its part file. Separate assigned shards
have separate filenames, so their Git merge is a file union. Choose enough
shards to keep individual files within the Git host's size limits; Core
does not commit, push, schedule workers, or repartition automatically.

For example, separate workers can run:

```sh
quail warm notes --field body --shard 1/4
quail warm notes --field body --shard 2/4
quail warm notes --field body --shard 3/4
quail warm notes --field body --shard 4/4
```

Each transfers its completed part through the project's normal Git workflow.

### Ingestion uses the same vector cache

On dataset open, scan finalized packs for the current dataset and source
version. Ingest compatible packs before spawning a kernel or starting an
explicit warm. Skip other embedding identities without treating their
presence as a project error. Newly pulled packs become visible on the next
open; no live watcher or cache-distribution service is needed.

Validate each candidate pack completely: schema version; path/header and
dataset/source agreement; embedding descriptor and identity; source field
selection; shard bounds; and sorted, unique hashes exactly covering the
declared range in that inventory. Validate strict base64, packed float32,
dimensions, finite coordinates, and nonzero norm through the same vector
validator used for provider results. Reject a malformed or truncated pack
as a whole, report its path and reason, and continue with other packs or
ordinary lazy embedding. Missing parts are always acceptable.

Validate and stage records outside a shared-index writer transaction. A
host TEMP table can stage a large file without retaining Python float
objects. After staging completes and its transaction closes, insert one
valid pack in a short shared-cache transaction, rechecking the established
dimension there. Never hold that writer while reading/decoding the file or
contacting a provider. An invalid pack contributes no vectors.

Use the existing `(embedding_id, text_hash)` key and canonical-insertion
rule; overlaps and repeated ingestion are harmless. Fields and shard counts
describe production and validation, not separate vector namespaces. Once
inserted, a compatible vector is reusable wherever that exact text occurs.
Partial pack sets simply leave some cache misses for later warm/query calls.
Keep packs derived and optional: no coordinator, completeness database,
alternate cache engine, or remote-cache API is required.

## 7. Kernel execution and confinement

The child opens the read-only index, initializes private tables, loads RE2
and optional NumPy, registers UDFs, and creates the user namespace before
confinement. Pass only the control descriptors it needs; it inherits no
host locks, run-log handles, or provider connections and receives no
provider credentials.

Use a scrubbed environment. Disable the cell-facing file API and install
the subtractive audit hook for filesystem mutations, new database
connections, sockets, process creation, native loading, and instrumentation
as described in `docs/kernel.md`. Permit read-only access under resolved
standard-library roots so ordinary imports work; disable bytecode writes.
Deny other file paths. On Linux attempt network namespace isolation and
report whether it succeeded. Do not introduce a Python module allow-list,
import-state framework, or general syntax allow-list.

Open SQLite read-only and install its authorizer before user code. Permit
the runtime's TEMP operations; deny main-schema writes, attach/detach,
extension loading, and writable pragmas. Kernel internals are outside the
public namespace. Core's confinement prevents ordinary accidental access;
determined Python introspection is outside its trust boundary. Hosted
supplies OS isolation for untrusted execution.

The cell runner parses once, executes statements, and displays the last
expression's repr when it is not None. Capture stdout, stderr, display,
and formatted traceback through the same bounded text sink. Retain only
the UTF-8 prefix that fits `output_kib`, count omitted bytes, append a
truncation notice, and set `truncated`. Formatting runs inside cell limits.
Do not accumulate unlimited output and truncate it afterward.

Keep the existing limit defaults:

| Limit | Enforcement |
| --- | --- |
| CPU: 30 seconds per cell | Child CPU timer and a normal cell error when handled; hard recovery remains available through the host |
| Wall: 120 seconds per cell | Host interrupts, then kills after a five-second grace period |
| RSS: 1024 MiB per kernel | Host monitors resident memory and kills an over-limit child |
| Output: 64 KiB per cell | Bounded capture plus a truncation notice |
| Retrieve: 1000 entries | Clamp retrieve only; values stays subject to RSS |

CPU and wall budgets reset per cell. A provider request pauses the cell's
wall budget, but each provider attempt and retry sequence is bounded.
Host memory monitoring and cancellation continue while the provider is
busy. Sample RSS through `/proc` on Linux or `ps` on macOS. A single
missed sample is not a limit failure; persistent inability to monitor
must fail startup or the host operation, not silently disable enforcement.
Do not use `RLIMIT_AS` as a substitute for RSS.

A normal exception rolls back private tags. CPU and wall expiry are latched
for that cell: catching the interrupt in user Python cannot turn an expired
cell into a success. If the child returns, it reports a limit error with
no tag delta. The host's wall deadline and grace period still bound a child
that does not return. A killed child is reconstructed from committed
cache/log state. The host never retries the Python that was interrupted.

Closing the host closes or terminates and reaps its child, then releases
locks. The child must also exit on loss of its control channel while a cell
is executing, so abrupt host death cannot leave an orphan kernel running.
This is parent/child lifetime handling, not a background supervisor.

## 8. Core operations and the CLI

`service.py` contains plain operations for initialization, import,
`setup(project)`, `open_session(...)`, listing, fork, fields, export,
and warm. It owns no kernel registry or process-global state.

`open_session(project, session, dataset=None, fork_from=None, *,
spawn=None, embed_fn=None)` returns a ready `Kernel`. The caller owns it.
`Kernel.exec(code)`, `reset()`, and `close()` own live operations.
The spawn and raw embedding callables are the only Hosted substitutions
needed initially; do not generalize them into plugin registries.

Use this command set:

```text
quail init [DIR]
quail import CSV [--name N] [--id COL] [--embed PROVIDER/MODEL --embed-revision R]
quail setup [--json]
quail exec SESSION FILE.py [--dataset D] [--fork-from S] [--json]
quail exec SESSION --stream [--dataset D] [--fork-from S]
quail sessions [--json]
quail fork SRC DST
quail fields DATASET [--session S] [--json]
quail export SESSION [--out PATH] [--json]
quail warm DATASET [--field F] [--shard I/N] [--json]
```

Use `argparse`. All commands except init discover the nearest
`quail.toml` from the working directory or its parents. One-shot commands
delegate to the same Core operations used by a live stream.

Export source fields in import order with canonical ID first, then tag
fields in deterministic name order. Preserve text and JSON-encode compound
tag values. Default to `exports/<session>.csv` and write atomically. A
supplied output path resolves from the invoking directory and may be anywhere
inside the project after symlink resolution. Reject an output that names or
aliases a registered source, the manifest, or the ignore file, or falls in
the managed session, index/lock, or warm-pack directories. Return the path,
rows, columns, and orphan count. Export is a report, not a lossless backup.

### Foreground execution

`quail exec SESSION --stream` owns one kernel. It writes a ready record,
then processes one complete JSON object per input line:

```json
{"op":"exec","code":"x = 10\nx + 1"}
{"op":"reset"}
```

Initial success is `{"ready":true,"session":"...","run":"..."}`,
with a `warnings` list when source changes or recovery need reporting.
Opening failure is `{"ready":false,"error":{...}}` followed by nonzero exit.
An execution response is:

```json
{"session":"study","run":"...","cell":1,"output":"11","error":null,"tags_written":0,"truncated":false,"kernel_restarted":false}
```

Reset returns `{"reset":true,"session":"...","run":"<new run>"}` once
the replacement is ready. Malformed requests return one serialized error
without executing or logging a cell and leave the stream open.

Cell failures are ordinary execution responses and keep the stream open.
Persistence, unrecoverable cache, or protocol failures close it with a host
error. There are no concurrent requests, request IDs, multiplexing,
standalone reset command, socket, or background supervisor.

The internal child protocol is equally small: ready, numbered run/result,
and bounded embedding request/response records while a cell is in flight.
It carries tag deltas and vector data that are never included in agent
output. Host-assigned run/cell identity is enough to associate results;
there is no general RPC system or retryable execution protocol.

Flush after each record. Stream stdout is JSONL only; human diagnostics
use stderr and cell output is inside its result. EOF, SIGINT, or a broken
output pipe closes the owned child in a finally block. Never interpret a
delivery failure after log sync as an annotation rollback.

The file form opens, executes once, prints the result, and closes. It exits
zero for a successful cell and nonzero for a cell or host failure, so shell
pipelines cannot mistake a failed analysis for success. Python variables
persist only in the stream; tags persist in both forms. Include opening
warnings in the file form's JSON result and human diagnostics too.

### Agent orientation

Setup returns `documentation`, dataset summaries, and session summaries
including ID/source compatibility and recovery diagnostics. It never starts
a kernel. Fields are included in dataset orientation; sessions report
history counts, last activity, source changes, and orphan tags. Unavailable
generated-ID history is identified without inventing an empty analysis.

Retain the structured CLI invocation metadata alongside `documentation`,
`datasets`, and `sessions` in setup's JSON result:

```json
{"interface":{"setup":"quail setup --json","open":"quail exec SESSION --stream [--dataset D] [--fork-from S]","exec":{"op":"exec","code":"..."},"reset":{"op":"reset"},"export":"quail export SESSION --json"}}
```

This describes invocation and remains consistent with the agent document;
it does not override that document's semantics.

Package the corrected canonical `docs/api.md` as `quail/data/api.md`
using Hatch's build inclusion. Load packaged data with a repository-tree
fallback for development, never from the caller's working directory.
Return that text exactly. The document will explain the local stream and
the language; Hosted may supply its own invocation wrapper later.
Do not maintain a second agent manual or ship the old semantics with a
runtime override banner. Reconcile the document before exposing the
completed CLI to agents.

## 9. Build and verification

Build useful paths through the system. The first runnable slice must
exercise the real host/child boundary and durable log; it need not have
semantic search.

| Slice | Deliverable | Proof |
| --- | --- | --- |
| 1 | Packaging, minimal project/import/index, CLI setup and persistent execution, Field reads, count/retrieve, tag, log replay | Initialize a small CSV project, inspect and tag it through the actual CLI, fail a cell, close, reopen, and recover the committed tags |
| 2 | Complete language, values/grouping, lexical search, entry behavior, fields/export/fork | Agent workflows run through the same engine; another session cannot change lexical scores |
| 3 | Limits, persistence failure recovery, locking and source/ID continuity | Concurrent local sessions work; stable-ID edits preserve sessions and positional IDs cannot reassign tags |
| 4 | Provider adapters, one cached embedding path, exact semantic scoring, local and shared warming | Warm/cold and NumPy/fallback agree; workers produce complete mergeable shards; imported packs reuse the same cache; a slow provider does not block another session's tag commit |
| 5 | Documentation alignment, installed-wheel and real-harness checks | A clean installation and the intended agent harness can keep variables, recover errors, export, pull shared vectors, and continue after cloning |

Each slice can be several small commits. Introduce the relevant guards
with the behavior they protect; slice 3 completes failure coverage rather
than licensing an unsafe first implementation. Create files as needed,
not as empty placeholders. Get a working CLI path before investing in
warming optimizations.

Use PEP 621 and Hatchling, a generated committed `uv.lock`, and
`quail = "quail.cli:main"` as the entry point. Use pytest, Ruff, and mypy
for development. CI installs the lock, runs checks and tests, and builds
a wheel. Once the CLI exists, install that wheel into a clean environment
and smoke-test it. Exercise the small platform-dependent lifecycle and
confinement surface on both supported OSes before claiming support;
no broad dependency or version matrix is needed.

Tests use temporary projects and real SQLite. Mock the provider boundary
and inject time, process failure, or placement only where needed.
Organize tests around these observable contracts:

| Contract | Essential cases |
| --- | --- |
| Project identity | Safe names and paths, exact text preservation, ID resolution, source-version changes, source edited during import, non-destructive metadata publication |
| Session scope | Stable-ID additions/edits/reorders and ID-column renames continue in the same session; deleted IDs count as final orphans and restored IDs recover tags; explicit preservation of generated IDs permits later edits; automatic positional reassignment fails; source/tag name conflicts are reported |
| Replay | Continuation on a clock behind imported history, deterministic concurrent ties, forked history, malformed records with valid later cells, interrupted headers/tails, persistent recovery diagnostics, failed/ignored-record digests |
| Durable completion | Child death before/after result, log append/fsync uncertainty, cache failure after log sync, host death before reply; never execute code twice |
| Private state | Read-your-writes, disk-backed tag working tables with bounded memory, newly created fields, failed-cell rollback of tags/FTS/derived search state, variables retained on normal failure |
| Concurrency | Two kernels read then tag without a shared snapshot upgrade; embedding waits coexist with another session's commit; exports see committed state |
| Language | Method/produce pairs, None and predicate negation, numeric and mixed-list comparison, recursive text conversion, container grouping without string collisions, Unicode case and whitespace, strict verb arguments, standard seed types and random-expression reuse |
| Search | Isolated field/session BM25, absent versus empty versus nonmatch, phrase handling, invalidation after tags, equivalent warm/cold scores |
| Embeddings | Full-value requests, Ollama truncation disabled, input ordering, finite packed vectors, dimension races, revision separation, bounded retries |
| Shared warming | Disjoint/balanced shard coverage, row-order-independent assignment, mixed shard-count composition, complete reused/new output, atomic publication, partial pack sets, whole-pack rejection, duplicate keys, address independence and revision separation |
| Runtime and CLI | Ready/stream/reset and setup invocation metadata, bounded output, CPU/wall/RSS failure including caught interrupts, parent/child cleanup, JSONL purity, recovery warnings, safe project-relative exports, exit status, actual harness variable persistence |

Run examples from the corrected agent document against a fixture that
supplies their assumed fields and values. Check an explicit public namespace.
Do not parse every inline code span as a required exported name.

Keep performance checks representative: source open/rebuild, a full-field
count, ranked retrieval, lexical search, semantic reuse, and a bulk tag
commit on a fixed corpus. Measure before adding another cache, persistent
mapping table, or planner optimization. Avoid machine-specific latency
promises.

The initial Core is complete when an agent can import, inspect, search,
annotate, recover, export, share warming work, and continue a session from
its text project, including source edits with stable IDs. This includes
the original workflows; build order does not make later slices optional.
Automatic identity remapping, a worker coordinator, distributed conflict
resolution tools, a daemon, extra backend/provider frameworks, and Hosted
policy remain outside Core.

## 10. Documentation alignment to do later

This editing step leaves other files untouched. Before publishing the
rebuild, move the settled behavior into the documents that own it and
remove the temporary precedence notice at the top of this guide.

| Owner | Required alignment |
| --- | --- |
| `docs/api.md` | CLI stream invocation; stable-ID continuity and positional-ID scope; tag-all with None; predicate-producing methods and absence; stored-field-only search; whole-value semantics and length failure; live Entry reads; container grouping keys and random-expression reuse; normal exception versus hard-crash output; corrected examples |
| `docs/storage.md` | Stable-ID re-import and orphan recovery; per-run source provenance; host-owned logs and logical ordering; visible best-effort replay; one durable commit point; private tag working tables; per-field FTS; shared warming and revised vector identity; local lock layout; project-relative exports |
| `docs/kernel.md` | Read-only source connection plus private TEMP transactions; host durability and cache operations; minimal control exchange; recovery diagnostics and outcomes; RSS and bounded output; CLI-only Core with setup invocation metadata; shared-warm orchestration; module ownership |
| `README.md` | Working CLI path, shared warming, stable-ID source updates, and current implementation status; no local MCP command |
| `AGENTS.md` | The corrected document ownership and dependency list; host-only remote embedding requests; self-contained prelude without log replay |

When those owners agree, this guide should explain how to build their
contract, not accumulate another series of overrides.
