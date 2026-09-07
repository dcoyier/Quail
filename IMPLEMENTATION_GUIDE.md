# Implementation guide

This is the implementation contract for the Quail core rebuild. Its goal is
a small environment in which an agent can inspect a corpus, write annotations,
and continue that work on another machine.

This guide owns the implementation contract; `USING_QUAIL.md` owns the
agent-facing language and operating instructions. `README.md` provides
installation and repository orientation.
Keep them consistent. When implementation settles a behavior differently,
update its contract and agent-facing documentation in the same commit.

The guide fixes observable behavior and ownership. It does not prescribe
the order of function definitions, a class for every concept, or a final
file count. Use ordinary modules and small records. Add machinery only when
a concrete behavior needs it. Simplicity comes from shared paths and clear
ownership; it is not permission to remove supported workflows.

Judge changes against the complete workflow: an agent clones a project,
discovers its data and prior work, builds reusable Python analysis, searches
and annotates efficiently, and shares both history and embedding work through
Git. Preserve that workflow while choosing the fewest mechanisms that serve
it. The implementation details below can be improved without narrowing it.

## 1. The design

An external agent submits one cell per CLI invocation. A local background
host owns one persistent Python child for that session; the client prints
the result and exits. The child evaluates the analysis language; the host
owns durable files and embedding requests. Hosted owns the same `Kernel`
directly through the Python API, without the local CLI transport.

Five rules carry the design:

1. **Each open kernel sees one source snapshot.** Stable entry IDs let a
   session continue across source edits. Generated positional IDs belong
   only to their source version and cannot silently move annotations.
2. **Tags are durable analysis state.** Python variables are working memory.
   A normal failed cell rolls back its tags and keeps its earlier assignments
   and captured output. Restarting the kernel discards working memory.
3. **The log decides what committed.** A host acknowledges completion only after
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
standard library, SQLite with FTS5, `google-re2`, and NumPy. Include NumPy in
the normal installation and use one vectorized scoring implementation;
agents should not need an extra installation step to get timely search.
There is no Core MCP dependency.

### Ownership

Organize around the boundaries that must stay independent: durable project
state, derived storage, analysis execution, and the adapters that invoke it.
A new transport should not change the language or commit path; a new
expression should not change process management. Start with these areas,
splitting cohesive responsibilities when that makes them easier to reason
about and test. The filenames are a starting layout, not a fixed file count.

All areas may use `contracts.py`; the final column lists other Core
dependencies. Imports follow this direction, without cycles.

| Area | Owns | Other Core dependencies |
| --- | --- | --- |
| `contracts.py` | Shared JSON value rules and text rendering, result/error and control records, pure codecs | None |
| `project.py` | Project configuration, paths, metadata publication, local locks | None |
| `history.py` | Run-log writing and validation, ordered replay, history digests and summaries | `project.py` |
| `index.py` | CSV import, source indexes, materialized tags, vector storage, warm-pack validation and ingestion, cache synchronization | `project.py`, `history.py` |
| `embed.py` | The two embedding HTTP dialects and the shared cached-embedding operation | `project.py`, `index.py` |
| `language/` | Expression construction and SQL compilation, evaluator, verbs, entries, private tag state, search preparation and scoring | None |
| `prelude.py` | Child bootstrap, control I/O, persistent namespace and cell runner, confinement | `language/` |
| `kernel.py` | Child lifetime, control exchange, limits, durable cell completion | `project.py`, `history.py`, `index.py`, `embed.py` |
| `service.py` | Project operations, the shared dataset-open path, session opening, export, local and shared warming | `project.py`, `history.py`, `index.py`, `embed.py`, `kernel.py` |
| `local.py` | Per-session host startup, local connections, request admission, and live inspection | `project.py`, `service.py`, `kernel.py` |
| `cli.py` | Argument parsing, presentation, exit status | `service.py`, `local.py` |

`contracts.py` is a small dependency-free vocabulary, not a general utilities
module. Share the value conversion and wire definitions used on both sides
instead of maintaining matching copies. Configuration belongs in
`project.py`, run schemas in `history.py`, and pack schemas in `index.py`.
They use the shared value rules, but retain their own format versions and
validation. Use small typed records and ordinary functions, not a schema
framework or an object hierarchy for every JSON shape.

Keep `quail/__init__.py` inert. The child still starts with
`[sys.executable, "-m", "quail.prelude"]`. Its dependency closure is
self-contained; it need not be one large file. Separate expression/compiler
code, evaluator/verb state, and search preparation within `language/`.
`prelude.py` delegates to them and loads the needed modules before installing
the audit hook. None imports host modules, parses manifests or logs,
performs provider HTTP, or writes durable files. Hosted can place this same
child package in its confined process. Replay has one implementation in
`history.py`; the host initializes working tags from the synchronized index.

SQL belongs in `index.py` for host storage and `language/` for the child's
queries and TEMP state. Transport code calls operations; it does not build
queries, replay logs, or decide tag commits. Sharing a wire codec does not
make a received message trusted: validate it at the process boundary and
check its scope in the owning operation.

### State and resource lifetime

Keep resolved configuration and source identity in immutable records.
Keep mutable state on its owner, with explicit references to the few
collaborators it needs. There is no process-global current project, session,
database connection, or search cache, and no general context/service locator.

`service.py` acquires resources for an operation and releases them when it
ends. A successful `open_session` transfers its connections, locks, and
run resources to the returned `Kernel`; a failed open unwinds what it
acquired. Use ordinary context managers for this ownership transfer and
cleanup. The local adapter separately owns its listener and client sockets.

Inside the child, one evaluator owns the connection, field catalog,
working tags, and derived search state. The namespace's verbs and
constructors, and its Entry handles, refer to that evaluator. The cell runner
owns the namespace, compiler flags, bounded output, and cell transaction;
it invokes the evaluator without embedding its query logic. Cell-local
write tracking and output end with the cell; the namespace, working tags,
and reusable caches remain until reset or close. Sections 3–7 define their
commit, rollback, and invalidation rules.

## 2. Projects and source versions

The durable project remains ordinary text:

- `quail.toml`: project, dataset, provider, and limit configuration.
- Dataset CSVs at their registered paths.
- `sessions/<name>/session.toml` and `sessions/<name>/log/*.jsonl`.
- Optional CSV exports beneath `exports/`.

Optional `warm/` packs carry derived embedding vectors between machines.
They may travel with the project through Git, but are not analysis truth;
missing packs never prevent opening or analyzing the source.

`.quail/` holds disposable SQLite files, local locks, and runtime sockets
and is gitignored. Remove it only when Quail processes using the project
are closed. Core never commits, pulls, or pushes anything.

Dataset and session names are non-empty single path segments; reject `.`,
`..`, separators, and NUL instead of rewriting them. Resolve project paths
against the manifest root and keep managed paths inside it. Source CSVs
cannot occupy the manifest, ignore file, or the managed `.quail/`,
`sessions/`, and `warm/` directories. Validate resolved paths before
creating files.

### Manifest and commands that create files

The manifest is editable text with this shape. Provider tables and
`[kernel]` are optional; the base URLs and kernel values shown are defaults.
Embedding configuration includes an explicit revision.

```toml
[project]
quail = "1"                       # manifest schema version

[datasets.notes]
source = "data/notes.csv"
id = "id"                         # optional; resolution is described below
embed = "ollama/embeddinggemma"   # optional; selects dialect and model
embed_revision = "study-model-v1"

[providers.ollama]
base_url = "http://127.0.0.1:11434"

[providers.openai]                # an OpenAI-compatible endpoint
base_url = "https://api.openai.com/v1"
api_key = "env:OPENAI_API_KEY"    # optional credential reference

[kernel]
cpu_seconds = 30
wall_seconds = 120
memory_mb = 1024
max_limit = 1000
output_kib = 64
```

Dataset paths resolve relative to the manifest. Embedding dimensions are
learned from vectors and are not a manifest setting. The `[kernel]` values
configure section 7's limits; they are defaults, not fixed product limits.

An embedding revision is a non-empty operator designation for fixed model
weights and embedding behavior. It is required when `embed` is configured;
it is not inferred from a mutable model name. Section 6 defines its use.
Without `embed`, lexical analysis works and semantic search gives a
configuration error.

Unknown keys fail clearly. Provider credentials are always `env:NAME`
references, resolved by the host immediately before provider HTTP, not for
orientation or cache hits. Never put literal secrets
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

An open kernel always keeps its source snapshot. Close affected kernels
before rebuilding changed data. If a new source field collides with a
session's recovered tag field, do not materialize or open that session until
the name conflict is resolved. Report it without blocking unaffected sessions
or hiding either field.

An existing session rejects `fork_from`; a supplied dataset must match.
A new session uses its requested dataset or the project's sole dataset.
When forking, the source session supplies its dataset and identity provenance;
reject any conflicting dataset argument. Ambiguity is an error. `quail info`
lists sessions and never allocates one.

Fork by copying a closed source session's logs into a new destination and
writing its metadata. Preserve run IDs and source/ID provenance. Hold the
source session lock through the copy, validate the copied logs under
section 3, and only then publish the destination. Log validation does not
require the historical CSV to be available.
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

On session open, synchronize its available history and obtain the largest
`order` across all validated complete cell records, including failed cells.
Reuse the cached maximum when the history digest matches; otherwise compute
it during replay. The next cell uses that number plus one; each recorded
cell advances it again.

Replay successful records in ascending `(order, run_id, n)` order. The last
write to an entry/field wins and null clears it. This is a small logical
clock: a run continuing observed history always writes after that history,
even when the machine's wall clock moves backward. No timestamp comparison,
vector clock, or distributed lock is needed.

Stream validation and replay rather than loading all historical code,
output, and deltas into a Python list to sort. Each run is already ordered;
`history.py` merges ordered run iterators and computes summaries.
`index.py` stages the resulting tags in private disk-backed state and
publishes them only after complete validation. This preserves the replay
policy without making memory grow with the entire transcript.

Concurrent runs on separate machines may choose equal logical orders;
the run ID breaks ties deterministically. This is a defined merge policy,
not a claim that concurrent coding decisions agree. All original writes
remain in the logs. Use separate named sessions or forks for independent
coding that must remain independently inspectable. Git merges their files;
it does not choose which analyst is right.

A live kernel uses the history it synchronized when opened plus its own
new records. Pulled or manually edited history is incorporated on the next
open, not halfway through a cell. Cache digests must describe only history
actually applied, never newly discovered records the kernel has not seen.

### Parsing and validation

Validate every newline-terminated header and cell: UTF-8 and JSON, supported
schema, scope, numbering, logical order, and tag values. Any invalid complete
record fails synchronization and opening of that session. Never skip the
record, exclude its run, or continue with later records after a gap. Cell
numbers start at 1 and are contiguous within each run, including failed
cells; logical orders increase within the run. Filename/header agreement
and numbering checks reject duplicate identities within a session. Forked
sessions may retain the same run IDs; no cross-session deduplication is needed.

Automatic recovery handles only interrupted appends:

- Treat a final fragment without a newline as an unfinished append and
  ignore it, even if its JSON appears complete. Complete records before it
  still apply when all complete records in the session validate.
- Treat an empty file or wholly unterminated header as an interrupted run
  creation with no cells. These files do not strand a new session. A complete
  invalid header remains an error.

Frame records on newline bytes before decoding, so a partial UTF-8 character
in the ignored tail cannot prevent reading earlier records. Leave every
original file untouched. Report ignored tails and interrupted run creations
through ordinary warnings with their file and location, including on cached
opens. Report an invalid complete record as an error with session, file,
line, and validation reason. An unsupported version should identify the
compatibility problem; do not guess what caused a record to be invalid.

Do not publish partial tags or an `applied` marker for history that failed
validation, or use an older cached materialization as fallback after that
failure. The affected session's open, export, and fork fail. `quail info` and
session listing report it as unavailable with its error, while other
sessions and source-only operations remain usable. Historical listings can
validate logs without the source. Live kernels retain section 4's committed
snapshot rules; external history edits take effect on the next synchronization.

Map valid historical writes to current stable IDs and report final orphan
tags separately. An ID missing from the current source is not a malformed
record. New child results are still checked against their live source before
logging. Source/tag name conflicts follow section 2's compatibility rule.

### History digests and materialization

The session digest is SHA-256 of canonical JSON containing sorted
`[log_filename, sha256_of_exact_file_bytes]` pairs. Include valid failed-cell
records, empty files, and ignored trailing fragments in the digest. The
digest is a cache marker, not an ordering or identity system. It is independent
of the session directory name, so copied history has the same digest.

Cache synchronization replays once when the digest or source version
differs from the stored `applied` marker, then replaces that session's
materialized tags, orphan count, maximum logical order, history summary,
and marker together after validation succeeds. The summary retains counts,
last activity, and interrupted-append warnings needed by info and open;
there is no summary of salvaged or excluded records. A matching digest in
the current cache schema avoids parsing old cells merely to rediscover
those facts. Hash files in a streaming pass; timestamps alone cannot prove
history unchanged. At runtime the host updates its own file hash and
summary incrementally, without re-reading past code.

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
5. In a short independent SQLite transaction, the host applies the delta,
   new digest, maximum logical order, and history summary together. No user
   Python or HTTP runs inside that transaction.
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
| Log write or fsync has an uncertain outcome | Stop the host, discard the child, and report a persistence failure with the run/cell identity; never append a contradictory failure record for that cell |
| Cache update fails after a synced record | The record remains committed; retry or reconstruct only the cache, never execute the code again or turn that committed cell into a rolled-back result |
| Client disconnects after acceptance | Finish the cell under its existing limits and retain its result in the run log; delivery failure does not roll back or repeat execution |
| Host dies before replying | Reopening recovers complete records; an unacknowledged cell may have committed, so do not automatically resubmit it |

After a log I/O failure, a later opener applies the validation and tail
rules above to the records actually on disk and syncs recovered files before
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

Set SQLite's transaction mode explicitly. The operation that owns an atomic
change owns its begin/commit/rollback; helpers must not silently commit a
caller's transaction. Connection lifetime and transaction lifetime are
different: keeping a kernel open does not require keeping a read transaction
open. Use the standard SQLite driver directly, with a small number of
explicit storage operations rather than an ORM or generic repository layer.

The shared index contains:

| Data | Representation and lifetime |
| --- | --- |
| Import metadata | Schema version, source hash/version, canonical ID resolution, ordered source fields |
| Source rows | `entries`, with import-order integer rowid and unique text `id` |
| Source lexical indexes | One single-column FTS5 table per non-ID source field |
| Materialized session tags | `tags(session, entry, field, value)`, keyed by session, stable entry ID, and field; value is canonical JSON |
| Applied history | `applied`, keyed by session: current source version, log digest, orphan count, maximum logical order, and history summary |
| Embedding vectors | `vectors(embedding_id, text_hash, vec)`, keyed by identity and exact text hash |
| Ingested packs | Local path/content-hash receipts for completed ingestion; disposable shortcuts for repeat ingestion |

Use foreign keys for tag IDs, an index on `(session, field)`, and
parameter binding for values. Generated FTS table names derive from field
names through a collision-resistant hash; CSV names never become
unquoted SQL. Tag caches exist only for sessions compatible with the indexed
source: supplied stable IDs preserve identity across versions, while
automatic positional identity requires the initial generated-ID version.

There is no shared `tags_fts`, passage table, or durable semantic mapping
format. Derive per-entry semantic mappings from immutable source text or
private tags, retain them within the kernel, and invalidate them when needed.

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

Finish bootstrap and cell transactions explicitly, on success and failure,
and consume or close internal cursors before returning. Saved expressions
and Entry handles must not retain open SQL cursors between cells. The dataset
lock preserves the source snapshot; an idle read transaction merely pins
old WAL pages and prevents checkpoint progress. Allow ordinary
[WAL checkpoints](https://www.sqlite.org/wal.html#concurrency) to advance
between cells without adding a checkpoint service.

The child may read vectors already visible in its read snapshot. Missing
vectors are obtained from the host and used directly from its response:
do not expect a long-lived read snapshot to see the host's new inserts.
The host checks the shared cache again when servicing a request.

One evaluator write path updates private tag values, present counts, any
existing field FTS index, and the cell's final write set in the same
transaction. It also advances affected field revisions and invalidates
cached Entry tag reads, semantic mappings, matrices, and score tables for
those fields. Every form of `tag` uses this path; verbs and search helpers
must not each invent their own invalidation rules.

A failed cell rolls back values, counts, and FTS, restores the field catalog,
and discards derived state for fields touched by the cell, including TEMP
score tables. Use monotonically advancing field generations; rollback or
clear/recreation must not reuse a generation for different values. Unrelated
source caches remain reusable. Track affected fields, not an inverse journal
for disposable caches.

Read-only host operations use materialized committed tags. They never
inspect the child's TEMP tables, so export cannot see a half-finished cell.

### One opening path

`service.py` owns dataset opening for info, sessions, fields, export,
warming, and kernel creation:

1. Discover and validate the manifest and selected dataset.
2. Check the index under a shared dataset lock.
3. If absent or stale, release shared access, take the dataset lock
   exclusively, recheck, and build a replacement.
4. Hold shared access for the operation, or for the lifetime of an opened
   kernel. Discover finalized warm-pack paths for that operation; actual
   ingestion waits until embeddings are needed, as specified in section 6.

An operation that will publish project or session metadata acquires the
project metadata lock before entering this path, preserving the lock order
below. Existing-session execution needs no metadata publication lock.

Rebuild source rows and FTS, preserve compatible vector rows when available,
and replay valid, compatible session histories by ID, recording orphan
counts. A log-validation or source-compatibility failure leaves that session
unmaterialized and unavailable; it does not abort rebuilding the source or
materializing other valid sessions. Keep unavailable sessions on disk and
visible in listings with the reason. A schema mismatch rebuilds a disposable
cache; it does not trigger user-data migrations.

Opening a session then takes its session lock, checks its ID/source scope,
synchronizes its log, and spawns the child. Validate configuration before
creating new session metadata. Kernel reset retains its locks, index,
source version, and resolved embedding configuration; reload configuration
by closing the host and opening the session again.

Session summaries, fields, and export acquire and synchronize a closed
session before reading. A validation failure returns that session's error;
it never exposes partial tags or substitutes the older cached result.
If its session lock is already held, read one committed WAL snapshot of
its cache without replay. Require an `applied` marker for the matching
source version; an owner still initializing an uncached session means
temporarily unavailable, not an empty analysis. This may show the previous
committed cell while the owner finishes publishing the next one. A response
already acknowledged by its owner must be visible to a subsequent read.
If the current source changed, require the affected kernels to close before
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

Keep the compact shape in `USING_QUAIL.md`: `Field`, `Random`, ordinary
numeric expressions, predicates, `count`, `retrieve`, `values`, `tag`,
and `fields`. There is one expression-to-SQL engine in `language/`.

### One query path

Separate describing a question from preparing and executing it. Constructors
produce inert expression nodes. When a verb or `entry[expr]` evaluates them,
the evaluator checks their current scope and field dependencies, prepares
each distinct search dependency, and uses the compiler to build SQL over
those prepared results. Search preparation may ask for embeddings; scalar
SQL UDFs must not perform I/O or recursively invoke verbs.

Compile expressions to parameterized SQL and reuse joins within a query.
Carry the value's kind and encoding with its compiled fragment: source
text, SQL numbers, and JSON-encoded tag/list values are distinct. Decode
JSON at explicit boundaries, never by guessing from string contents or by
discarding its type through `json_extract` before a typed comparison.
Python UDFs implement individual operations SQLite cannot faithfully
provide; they are not a second row-by-row execution engine.

Share selection, ordering, parameter binding, and result decoding across
the verbs and Entry expression reads. A verb chooses its projection and
result shape; it does not maintain its own interpretation of an expression.
A small query record containing SQL, parameters, and dependencies is enough.
Keep the compiler inspectable and directly testable, without an optimizer
framework, multiple intermediate languages, or per-backend implementations.

### Ordinary Python is the extension mechanism

Variables, imports, functions, closures, classes, instances, and saved
expressions persist across cells in one kernel. Support nested expressions,
comprehensions, loops, decorators, and ordinary standard-library helpers.
An analyst can build a reusable class around the verbs without subclassing
a Quail engine or registering a callback. `values` hands computed columns
to Python; `retrieve` hands it entries; `tag` brings results back into durable
analysis. Keep this path usable when a transformation does not fit the DSL.

Use one persistent module namespace as both globals and locals, with normal
builtins subject to the capability restrictions in section 7. Register its
module identity so classes, dataclasses, and annotations work normally;
preserve future-import compiler flags across cells. Do not serialize the
namespace between requests or reconstruct it from a whitelist of values.
Reset and process replacement discard it; logs never auto-execute old code
to recreate variables. Reusable helper definitions can be kept as ordinary
analysis scripts in the project and submitted again by the agent.

Expressions are reusable descriptions, not frozen query results. Snapshot
mutable literal arguments when constructing them, so later mutation of a
Python list does not silently rewrite a saved predicate. References to tag
fields read the current working values at evaluation. Keep internal cache
keys separate from overloaded comparison operators and include the source,
embedding identity, and relevant tag-field revisions where applicable.

Expression construction inspects the cached field catalog and type
information, but reads no rows and performs no search or embedding.
Reject unknown fields and invalid method/produce pairs at construction.
An expression referring to a tag field must also be checked when evaluated,
since rollback or clearing its last value can remove that field.

Keep one method-signature table for construction checks. Methods that return
predicates are actually `Predicate` objects. Expressions and predicates
reject Python truth testing and iteration; `&`, `|`, and `~` compose
predicates. Preserve ordinary Python `is`/`is not`: a helper's
`if optional_filter is None:` and `entry["body"] is None` must work.
Identity tests inspect the Python object and cannot express per-entry
absence; use `Field("f") == None` for that. Verbs reject a plain bool where
they require a Predicate, with a hint
about symbolic comparisons. Do not implement a syntax ban or AST rewriting
of identity checks. This keeps Python reusable without pretending identity
can be overloaded.

For example, this ordinary class packages a reusable analysis, with no new
Quail abstraction. Its instances and expressions remain usable in later cells:

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
# A later cell uses the same objects and the same core verbs.
print(count(review))
tag(review, "topic:parking", True)
parking.sample(limit=3)
```

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

- `where` is None or a Predicate; `rank` is a number expression or None.
- `retrieve` defaults to 10. Limit and offset are nonnegative integers
  excluding bool. Clamp only retrieve's limit to `max_limit` and say so in
  the cell output, the same way truncation is noted.
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

### Execution should follow the size of the work

All verbs execute inside the kernel; only missing embeddings cross to the
host. Keep ordinary operations set-based and reuse prepared search nodes
throughout a query. Compile regexes once per distinct pattern/flags, not
once per row. Construction, `fields()`, and saved-expression reuse must not
accidentally scan a corpus. Maintain source present counts at import and
private tag counts alongside writes and rollback.

`count` without grouping stays a SQL count. Grouping streams selected values
into its Counter without a second full row list. `retrieve` selects the
requested IDs/rank, then fetches their source cells and tags in bounded
batches. Populate live Entry handles from those batches; printing ten rows
must not issue one query per displayed cell. Invalidate cached tag reads
after writes and rollback; source reads remain reusable. `entry[expr]` uses
the same prepared search state, but `values(expr, ...)` remains the bulk
path for computed columns.

For `tag(predicate, field, expression)`, resolve the target/value set into
a private TEMP staging table before applying it in batches. Do not retain
an unnecessary full Python copy or issue a separate SELECT per target.
Entry-list/literal writes use the same write path. Repeated `tag(entry, ...)`
calls in a Python loop are also supported: they share the cell transaction,
field indexes, and one final log fsync. This makes custom Python annotation
practical without adding a second bulk-write API or arbitrary Python UDF
registration. Reuse staging tables and parameterized statements instead
of creating and dropping tables for each Entry write. Maintain counts and
the final write set incrementally through section 4's shared mutation path;
do not rescan all tags after each call or diff the entire session to discover
the cell's delta. The final delta still has to fit the cell's memory budget.

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
embedding configuration for each open kernel and record the identity in
its run header. Core validates vectors; it does not attest remote weights.
A revision is an explicit reproducibility obligation, not evidence that
two arbitrary endpoints are equivalent.

Hash exact rendered UTF-8 text with SHA-256, prefixed `sha256:`. The host owns
one cached embedding operation used by both kernel requests and `quail warm`:

1. Deduplicate inputs, preserving the mapping back to request order.
2. Ingest discovered shared packs on first use, then read existing
   `(embedding_id, text_hash)` vectors.
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

Within `embed.py`, keep the raw provider call separate from the cached
operation that composes it with `index.py`. The two HTTP adapters use the
standard-library client, preserve request order (including indexed OpenAI
response items), and use finite request timeouts with a small fixed retry
bound for transport, rate-limit, and server failures. Do not retry
authentication, invalid-input, dimension, or schema errors.

`index.py` owns cache reads, vector validation, and writes; `embed.py` owns
the miss/batch/provider orchestration. Both `kernel.py` and `service.py`
call that cached operation. Warming needs no `Kernel` instance, child,
or process-lifecycle import. Hosted's `embed_fn` substitutes only the raw
provider call and still uses the same cache and validation. A cache batch
may survive a failed cell: vectors are derived operational state, not
annotations.

### Reuse and bounded scoring

Prepare a searched field on demand, never at expression construction or
ordinary session startup. The first evaluated semantic search may need to
embed that field's distinct complete values; subsequent queries reuse them.
Query vectors use the same exact-text cache. A reused query must need no
provider request when all of its vectors are already cached, even if the
provider is currently unavailable.

Use NumPy matrix/vector operations on packed float32 data. Normalize with
numerically safe norms and score each distinct text once, then map scores
to entries. Check agreement against a small scalar cosine reference in
tests; a second production scorer is unnecessary. Do not promise bitwise
identity across numerical libraries or provider recomputations. Equivalent
warm/cold inputs must agree within a documented numerical tolerance.

Reuse field mappings and normalized matrices across cells while they fit
within a bounded fraction of the kernel's memory budget. Keep larger
mappings and scores in indexed, file-backed TEMP tables and score vectors
in bounded NumPy batches. Evict disposable matrices before they crowd out
Python working memory; a corpus larger than the matrix budget must still
be searchable through the same exact scorer. Bound transport batches by
bytes as well as item count. Carry base64 packed float32 vectors over the
internal channel instead of expanding a corpus into JSON numbers. Use host
responses directly when the child's read snapshot predates cache inserts.

Budget retained matrices, mappings, Entry data, SQLite page caches, and
temporary normalization/batch allocations together, leaving room for the
analyst's Python objects. Several separately "bounded" caches must not each
assume the whole allowance is theirs. Check sizes before allocating; the
bounded path reads and scores batches without first building a full matrix.
Use the same normalization and scoring functions for resident and streamed
batches. Internal batch sizes and eviction policy are tuning choices, not
new public settings or alternative engines.

Prepare each distinct search node once for a verb and reuse its scores in
filtering, ranking, and value reads. Keep a bounded cache of recent score
tables across cells: `count(score > cutoff)`, `retrieve(rank=score)`, and
`entry[score]` should reuse unchanged scores, including when an equivalent
expression is constructed again. A plain internal structural key suffices.
Tag writes invalidate only affected field mappings/scores, and rollback
discards affected derived state. Updating `topic` must not rebuild a source
`body` matrix. Lexical corpus statistics still use the complete field;
neither cache eviction nor filtering may redefine the corpus or answers.
Keep a verb's prepared tables valid until its queries finish; eviction must
not remove a table still referenced by that operation.

Batch cold provider requests and report bounded progress on stderr for
first-time embedding or pack ingestion, including reused/new counts. Keep
CLI stdout's final-result contract. No per-row HTTP, repeated full-field
scoring from Entry access, eager warming of unrelated fields, or speculative
query planner is needed. Section 7's bounded provider I/O serves requested
work; it does not schedule background warming.

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
using the same text conversion and hashing as semantic search. Reuse it for
packs with the same field selection within an operation; large inventories
can use host file-backed TEMP storage. Sort by text hash. For `M` values and
one-based `1 <= I <= N`, select indices:

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
Use distinct assigned parts; two workers replacing the same part can still
create an ordinary Git conflict. The receiving agent pulls or merges the
files and uses semantic search normally. No import command, SQLite transfer,
or completed set of all parts is required.

Make the GitHub path usable in practice: report estimated/final part bytes,
warn above 50 MiB, and refuse to publish a part above GitHub's 100 MiB regular
Git limit, with a suggested larger shard count. Estimate as soon as dimensions
are known and check actual bytes before publication; completed local vectors
remain reusable on a retry. These are the
[GitHub file limits](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github),
not a limit on the total warm inventory. Keep enough independent parts to
use ordinary Git without requiring LFS or a second transport.

### Ingestion uses the same vector cache

On dataset open, discover finalized pack paths for the current dataset and
source version. The first cached-embedding operation ingests compatible packs
before deciding which provider work is missing; explicit warming uses that
same path. `quail info`, lexical analysis, and opening a kernel do not decode
vector packs or contact providers. Skip other embedding identities without
treating their presence as a project error. Newly discovered paths become
visible on the next open; no live watcher or cache-distribution service is
needed.

Validate each candidate pack completely: schema version; path/header and
dataset/source agreement; embedding descriptor and identity; source field
selection; shard bounds; and sorted, unique hashes exactly covering the
declared range in that inventory. Validate strict base64, packed float32,
dimensions, finite coordinates, and nonzero norm through the same vector
validator used for provider results. Reject a malformed or truncated pack
as a whole, report its path and reason, and continue with other packs or
ordinary lazy embedding. Missing parts are always acceptable.

Validate and stage the complete pack outside a shared-index writer
transaction. A host TEMP table can stage a large file without retaining
Python float objects. After validation and staging commit, insert vectors
through the existing bounded-batch cache path, checking dimension agreement
inside each short writer transaction. Never hold that writer while decoding
the file or contacting a provider. An invalid pack contributes no vectors;
an interruption while ingesting an already-validated pack may leave useful
cached batches, just as interrupted local warming does.

Publish a local `(relative_path, file_hash)` ingestion receipt with the final
batch, after every earlier batch committed. Streaming-hash an already-known
file and skip JSON decoding, inventory validation, and reinsertion when its
receipt matches. Hash and validate the same bytes for a new/changed file. Discard
receipts when their vector cache is discarded; never treat file size or
mtime alone as evidence of valid content. These receipts only avoid repeated
work and say nothing about other workers or project completeness.

Use the existing `(embedding_id, text_hash)` key and canonical-insertion
rule; overlaps are harmless. Fields and shard counts describe production,
not separate vector namespaces. An inserted vector is reusable wherever
that exact text occurs. Missing parts leave ordinary cache misses; deleting
a pack need not evict its cached vectors. No coordinator, shared completion
manifest, alternate cache engine, or remote-cache API is required.

## 7. Kernel execution and confinement

### Host execution ownership

Keep `Kernel.exec`, `reset`, and `close` synchronous and serialized. One
execution owner manages a Kernel and its SQLite connections through open,
execution, and close. Local connection handling may run separately, but
must not give every client thread direct access to that mutable state or
disable SQLite's thread checks to make it work. Publish small immutable
runtime snapshots for inspection, updated at the normal lifecycle and
completion transitions; inspection does not enter the executing child.

Blocking provider I/O must not prevent limit monitoring, child-liveness
checks, or local status responses. Use bounded standard-library workers
where needed; a provider worker returns data to the execution owner, which
validates and writes the cache. Keep at most one provider batch in flight
per Kernel initially. This supplies responsiveness without making the Core
API asynchronous, adding a cell queue, or sharing connections across workers.

### Child bootstrap and confinement

Pass only the control descriptors the child needs; it inherits no host
locks, run-log handles, or provider connections and receives no provider
credentials. Use a scrubbed environment.

On Linux attempt `os.unshare(CLONE_NEWUSER | CLONE_NEWNET)` while the child
is still single-threaded, before loading numerical libraries or starting
monitor threads, and record whether it succeeded. The
[user-namespace operation](https://man7.org/linux/man-pages/man2/unshare.2.html)
requires a non-threaded caller; trying only after NumPy initializes can
defeat this protection unnecessarily.

Choose a small, deliberate native-thread budget before importing NumPy.
Its [BLAS backend](https://numpy.org/doc/stable/reference/global_state.html#number-of-threads-used-for-linear-algebra)
may otherwise allocate a machine-sized pool for every kernel. Tune this
internal default using concurrent-kernel throughput and CPU use as well as
single-cell latency; a faster isolated multiply can make several sessions
slower together. No thread-pool controller dependency or public tuning API
is needed.

Then load the child package, RE2 and NumPy, create the evaluator on the
read-only index with its private tables, register UDFs, and create the
user namespace.
Disable the cell-facing file API and install the subtractive audit hook
before reporting the child ready:

- For `open`, permit only read access under resolved standard-library roots
  and the package roots of the preloaded NumPy and RE2 modules. Reject
  writes and other paths; disable bytecode writes. These read permissions
  let ordinary imports and NumPy's lazily imported submodules work.
- Deny filesystem mutation events, including `os.remove`, `os.rename`,
  `os.mkdir`, `os.rmdir`, and `shutil.*`; new connections through
  `sqlite3.connect`; and `socket.*`.
- Deny process creation through `subprocess.Popen`, `os.system`, `os.fork`,
  `os.exec`, and `os.posix_spawn`; native access through `ctypes.*`; and
  instrumentation through `sys.addaudithook`, `sys.setprofile`, and
  `sys.settrace`.

NumPy is available to analysis code from the preloaded module; its file
and network operations receive no additional capabilities. SQLite uses the
connection opened during bootstrap and manages its private temporary files
below Python's audit layer. Do not introduce a Python module allow-list,
import-state framework, or general syntax allow-list.

Open SQLite read-only and install its authorizer before user code. Permit
the runtime's TEMP operations; deny main-schema writes, attach/detach,
extension loading, and writable pragmas. Kernel internals are outside the
public namespace. Core's confinement prevents ordinary accidental access;
determined Python introspection is outside its trust boundary. Hosted
supplies OS isolation for untrusted execution.

### Cell execution and errors

The cell runner parses once, executes statements, and displays the last
expression's repr when it is not None. Capture stdout, stderr, display,
and formatted traceback through the same bounded text sink. Retain only
the UTF-8 prefix that fits `output_kib`, count omitted bytes, append a
truncation notice, and set `truncated`. Formatting runs inside cell limits.
Do not accumulate unlimited output and truncate it afterward.

Quail errors have type `QuailError`, a message, and an optional actionable hint.
Ordinary Python exceptions retain their type name and message, with a null
hint. Format tracebacks with the runtime's own frames removed, preserving
cell and user-helper frames so the agent can locate its error.

### Limits and shutdown

Use the configurable `[kernel]` defaults from section 2:

| Limit | Enforcement |
| --- | --- |
| CPU: 30 seconds per cell | Child CPU timer and a normal cell error when handled; hard recovery remains available through the host |
| Wall: 120 seconds per cell | Host interrupts, then kills after a five-second grace period |
| RSS: 1024 MiB per kernel | Host monitors resident memory and kills an over-limit child |
| Output: 64 KiB per cell | Bounded capture plus a truncation notice |
| Retrieve: 1000 entries | Clamp retrieve only; values stays subject to RSS |

CPU and wall budgets reset per cell. Only provider HTTP waits pause the
cell's wall budget; local preparation, pack ingestion, and scoring consume
it. Each provider attempt and retry sequence is bounded.
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
An exiting CLI client is not host death: the local host and its child remain
alive until explicitly closed or terminated. There is no idle expiry that
silently discards working memory.

## 8. Core operations and the CLI

`service.py` contains plain operations for initialization, import,
`info(project)`, `open_session(...)`, listing, fork, fields, export,
and warm. It owns no kernel registry or process-global state.

`open_session(project, session, dataset=None, fork_from=None, *,
spawn=None, embed_fn=None)` returns a ready `Kernel`. The caller owns it.
`Kernel.exec(code)`, `reset()`, and `close()` own live operations.
The spawn and raw embedding callables are the only Hosted substitutions
needed initially; do not generalize them into plugin registries.
`local.py` is a local caller of this API, not another execution engine.

Use this command set:

```text
quail init [DIR]
quail import CSV [--name N] [--id COL] [--embed PROVIDER/MODEL --embed-revision R]
quail info [--json]
quail exec SESSION -c CODE [--dataset D] [--fork-from S] [--json]
quail exec SESSION FILE.py [--dataset D] [--fork-from S] [--json]
quail exec SESSION --reset [--json]
quail exec SESSION --close [--json]
quail sessions [--json]
quail fork SRC DST
quail fields DATASET [--session S] [--json]
quail export SESSION [--out PATH] [--json]
quail warm DATASET [--field F] [--shard I/N] [--json]
```

Use `argparse`. All commands except init discover the nearest
`quail.toml` from the working directory or its parents. Each exec invocation
selects exactly one of `-c`, a file, `--reset`, or `--close`; dataset and fork
options apply only to code submission. Other commands call the plain Core
operations, with local inspection added as described below.

### From download to the first analysis

Treat this as a release acceptance path, not an aspirational README example.
It is for the implemented rebuild once available on the default branch;
the current design-only branch cannot run it yet. The eventual README must
name the usable revision and link to [uv installation](https://docs.astral.sh/uv/getting-started/installation/).
README owns installation; `USING_QUAIL.md` continues with the first study
and analysis. The combined path below must work without project inspection.
With Git and uv installed, no separate Python, database, embedding server,
MCP configuration, or hand-built manifest is required for lexical analysis:

```sh
git clone --depth 1 https://github.com/dcoyier/Quail.git
cd Quail
uv sync --locked --no-dev --python 3.12
. .venv/bin/activate
quail init ../study
cd ../study
cat > notes.csv <<'CSV'
id,body
n1,The parking permit is too expensive.
n2,The staff were helpful.
CSV
quail import notes.csv
quail exec first-pass -c 'body = Field("body"); parking = body.lexical("parking") > 0; count(parking)'
quail exec first-pass -c 'tag(parking, "topic", "parking"); count(by=Field("topic"))'
quail export first-pass
quail exec first-pass --close
```

The first result is `1`; the second uses the saved predicate and commits one
tag. Each command can be a separate shell tool call; no persistent stdin,
terminal setup, or manual host launch is required. Wait for the current
command's result before submitting another cell. If the harness backgrounds
a long-running command, use its normal wait/output facility to finish
reading that client. For shell calls that may not retain virtual-environment
activation, use the installed environment's absolute executable path or the
absolute invocation strings returned by optional `quail info`.

Once Quail is installed, continuing a cloned **study repository** needs only
`quail exec EXISTING_SESSION -c 'fields()'` from that project. Use
`quail info --json` when the dataset or session needs to be discovered;
it lists actual session names and code-submission commands.
Source indexes and tags rebuild automatically; shared packs are consumed
when semantic search needs them. Do not ask the agent to run init, re-import
registered CSVs, rebuild a database, or warm an already-shared corpus.
After a kernel restart, recover durable tags and resubmit needed helper
definitions; arbitrary Python objects do not survive process loss.

Explain semantic configuration as the next step after this working path:
choose an available embedding provider/model and a fixed revision through
import options or the manifest, then evaluate a semantic expression.
Warming is optional preparation and parallel sharing, never an admission
requirement. `quail info` should indicate whether semantics are configured
without requiring an available provider or resolving credentials just to
inspect data.
The cloned-checkout recipe and an installed-wheel equivalent must both work
from a study directory outside the Quail checkout.

### Export

Export source fields in import order with canonical ID first, then tag
fields in deterministic name order. Preserve text and JSON-encode compound
tag values. Default to `exports/<session>.csv` and write atomically. A
supplied output path resolves from the invoking directory and may be anywhere
inside the project after symlink resolution. Reject an output that names or
aliases a registered source, the manifest, or the ignore file, or falls in
the managed session, index/lock, or warm-pack directories. Return the path,
rows, columns, and orphan count. Export is a report, not a lossless backup.

### Execution and results

`-c CODE` and `FILE.py` submit one cell to the same persistent session kernel.
The client reads a file as UTF-8 before starting or contacting the host.
Starting a stopped session restores committed tags and creates fresh Python
working memory; an existing host retains its namespace, source snapshot,
configuration, and caches. Dataset and fork arguments must still obey
section 2 when attaching. There is no separate fresh-kernel file mode.

Default stdout is notebook output: captured prints, the final expression's
value when not None, and the traceback on failure. Host warnings and progress
use stderr. The client exits zero on success and nonzero on a cell or host
failure; ordinary cell failures leave the kernel usable. With `--json`,
stdout is one final result object instead of rendered notebook output:

```json
{"session":"study","run":"...","cell":1,"output":"11","error":null,"tags_written":0,"truncated":false,"kernel_restarted":false,"warnings":[],"limits":{"cpu_seconds":30,"wall_seconds":120,"memory_mb":1024,"max_limit":1000,"output_kib":64}}
```

`warnings` carries opening warnings, including source changes and ignored
interrupted appends, plus a fresh-start notice when this invocation starts
a kernel. Report child replacement through `kernel_restarted` and human
diagnostics. `limits` reports the settings actually applied by the host,
even if the manifest has since changed. Report the run/cell
identity on execution-related host errors when known. Failures before a
cell is accepted return an error without inventing a cell record or result.
Invalid CLI arguments use ordinary usage errors. There is no public stdin
protocol or terminal-mode handling.

Exec, reset, and close are mutually exclusive live operations. While one is
in progress, another fails clearly as busy before executing anything;
read-only status remains responsive, including during provider waits.
There is no queue. `--reset` requires an existing session: replace its live
child, or open it if stopped, and return the new run identity once ready.
A live reset retains source, configuration, and locks; opening a stopped
session loads them normally. Tags remain. `--close` stops the local host;
when no owner is running it succeeds without starting one or creating a
session. Neither operation executes a cell. Their JSON results contain
`reset:true` with session, new run, warnings, and applied limits, or
`closed:true` with session. Human output confirms the same outcome.

An accepted cell finishes under its existing limits even if the client
disconnects, is interrupted, or loses stdout. Completion still follows
section 3; sending the result is not its commit point. Never automatically
resubmit Python. If the original client is still running, read its eventual
result. Otherwise inspect live status and the run log using the run/cell
identity and submitted code before deciding whether to execute again.
A host failure closes the child and leaves recovery to the ordinary open
path; a missing response never proves rollback.

### Local host lifetime

`local.py` connects the short-lived client to a host scoped to the resolved
project and session. On demand, launch it with the same Python installation,
detached from the client's terminal and standard descriptors. The launching
client waits for readiness or a concrete startup error; connection retries
must not impose a short deadline on valid indexing or replay. The host owns
one `Kernel` through `open_session`; Hosted continues to own that API
directly. Keep the client lightweight, loading host-only modules on host
paths: attaching must not import NumPy, re-index data, replay history, or
rebuild the kernel. There is no service installation or machine-wide manager.

Use a private Unix socket beneath `.quail/`, with owner-only access. Bind
and connect using a short name relative to its containing directory so long
project paths do not exceed the platform's socket-address limit.
The existing session lifetime lock is the ownership authority. Only its
holder may publish or replace the session endpoint, after initialization.
Concurrent starters must converge on that owner or fail clearly; they must
not unlink its socket or create competing kernels. Connection permission
errors, timeouts, or an owner still initializing are not proof of a stopped
session. Preserve an unreachable owner's files and report the problem.
Check project/session and protocol compatibility when connecting; an
incompatible running host requires an explicit close, not replacement.

Use one framed request per client connection: exec, reset, close, or private
status. Malformed or incomplete requests execute and log nothing and do not
disturb the kernel. Reuse Core results and errors. Before execution, send
the host-assigned run/cell identity privately to the client for diagnostics;
delivery failure does not cancel accepted work. Forward bounded host progress
to client stderr without mixing it into the final stdout result. Slow or
disconnected clients must not block completion or inspection. Bound
connection attempts, but do not impose a fixed short response timeout on
an accepted cell; its existing execution/provider limits govern the wait.
No request IDs, retry protocol, or additional durable result store is needed.

The internal child protocol remains ready, numbered run/result, and bounded
embedding request/response records during a cell. Tag deltas and vector data
never appear in agent output. `kernel.py` still assigns run/cell identities
and owns completion; the local adapter only observes and transports them.

On close, stop admitting work and remove the endpoint while still owning
the session lock. Call `Kernel.close()` to close/reap the child, clean up
scratch and connections, and release locks before replying successfully;
then exit the host normally. A cleanup failure is an error, not a successful
close. An exiting host must never remove a successor's endpoint. Closing
the CLI connection alone does none of this. Keep hosts alive until explicit
close or termination; do not add a supervisor that restarts failed hosts.

### Project inspection and the usage manual

`info(project)` and `quail info [--json]` return configured `limits`, dataset
summaries, and session summaries including ID/source compatibility and
interrupted-append warnings. Inspection is optional: it creates no session
and starts no kernel, and opening validates current state without a prior
`info` call. The result contains project information and invocation metadata;
it does not include the usage manual. Fields are included in dataset
orientation; valid sessions report history counts, last activity, source
changes, and orphan tags.
Report a session with invalid history or incompatible source as unavailable
with its error, without inventing an empty analysis or treating partial or
stale counts as current. The rest of the orientation remains usable.

The local CLI augments info and session listings with `runtime` status from
`local.py`: `stopped`, `idle`, `busy`, or `unavailable` with a reason when an
owner cannot be inspected. Live status includes the active `run`, current
`cell` (null when none), `last_completed` as a run/cell pair or null, and
applied `limits`. These are observations, not reservations for a later
command. A completed cell becomes visible only through section 3's normal
completion path, including for a disconnected client. Read its code/output
from the existing run log; status is not another result store.

Inspection never starts a host or runs Python. Keep live status responsive
during execution and collect it independently of index synchronization:
if a changed source blocks ordinary inspection, retain runtime status
alongside that error so the agent can identify what needs closing. A missing
socket with a held lifetime lock is unavailable, not stopped. `info(project)`
and Hosted do not need to discover local sockets.

Retain the structured CLI invocation metadata alongside `limits`, `datasets`,
and `sessions` in the JSON result (shown with a compact `quail` prefix here):

```json
{"limits":{"cpu_seconds":30,"wall_seconds":120,"memory_mb":1024,"max_limit":1000,"output_kib":64},"interface":{"info":"quail info --json","exec":"quail exec SESSION -c CODE [--dataset D] [--fork-from S] [--json]","file":"quail exec SESSION FILE.py [--dataset D] [--fork-from S] [--json]","reset":"quail exec SESSION --reset [--json]","close":"quail exec SESSION --close [--json]","export":"quail export SESSION --json"}}
```

This describes invocation and remains consistent with the agent document;
it does not override that document's semantics. Top-level `limits` reports
the current manifest's resolved values, including defaults; a running host
continues using its applied limits, reported under its session's runtime
and in execution results, until it is closed.

Generate runnable invocation strings from the current absolute
`sys.executable` plus `-m quail.cli`, quoting arguments for the supported
shells. Support that standard module entry point alongside the `quail`
console script. An agent following `info` must launch the same installation
from a new shell without activating a venv, guessing a PATH entry, or
rediscovering the checkout. This is command metadata, not another interface.

Package the canonical `USING_QUAIL.md` as `quail/data/USING_QUAIL.md` using
Hatch's build inclusion. Make that exact text available with the installed
runtime, with a repository-tree fallback for development, never loading
from the caller's working directory. The document labels local operating
instructions separately from the shared language and session semantics.
Hosted may deliver the same manual through its own invocation wrapper;
its MCP delivery mechanism is not specified here. Do not maintain a second
agent manual or ship stale semantics with a runtime override banner. Run
the document's examples in the test suite (section 9) so it cannot drift
from the completed CLI.

## 9. Build and verification

Build useful paths through the system. The first runnable slice must
exercise the real host/child boundary and durable log; it need not have
semantic search.

| Slice | Deliverable | Proof |
| --- | --- | --- |
| 1 | Packaging, minimal project/import/index, CLI info and persistent execution, Field reads, count/retrieve, tag, log replay | Through separate shell calls, initialize a small CSV project, reuse variables/functions/classes across inline and file cells, fail a cell, close, reopen, and recover committed tags; verify this path in the target agent harness |
| 2 | Complete language, values/grouping, lexical search, entry behavior, fields/export/fork | Agent workflows run through the same engine with bulk database operations; another session cannot change lexical scores |
| 3 | Limits, persistence failure recovery, locking and source/ID continuity | Concurrent local sessions work; stable-ID edits preserve sessions and positional IDs cannot reassign tags |
| 4 | Provider adapters, one cached embedding path, exact semantic scoring, local and shared warming | Warm/cold and bounded-batch scoring agree; repeated queries reuse scores; workers produce complete mergeable shards; a slow provider does not block another session's tag commit |
| 5 | Documentation alignment, installed-wheel and real-harness checks | Run the download-to-analysis recipe; the actual harness preserves Python objects, recovers errors, closes cleanly, exports, and continues a cloned project with shared vectors |

Each slice can be several small commits. Introduce the relevant guards
with the behavior they protect; slice 3 completes failure coverage rather
than licensing an unsafe first implementation. Create files as needed,
not as empty placeholders. Get a working CLI path before investing in
warming optimizations.

### Engineering checks

Use PEP 621 and Hatchling, a generated committed `uv.lock`, and
`quail = "quail.cli:main"` as the entry point. Use pytest, Ruff, and mypy
for development. CI installs the lock, runs checks and tests, and builds
a wheel. Once the CLI exists, install that wheel into a clean environment
and smoke-test it. Exercise the small platform-dependent lifecycle and
confinement surface on both supported OSes before claiming support;
no broad dependency or version matrix is needed.

Apply linting and type checking from the first slice. Type the internal
operation boundaries and use strict mypy checking for Core; keep unavoidable
dynamic typing at the user-namespace and external-library boundaries instead
of propagating unstructured dictionaries or `Any` through the implementation.
Use narrow, explained exceptions to checks, not whole-module exclusions.

Keep format decoding, domain validation, execution, and presentation
distinct. Catch expected failures where they can be handled; convert them
to result/error records at the owning boundary. Unexpected implementation
failures must remain diagnosable, not become empty query results, absent
values, or a successful operation. Comments should explain ownership,
transaction, and invalidation decisions where they are implemented.

Install the locked NumPy dependency normally. Keep a scalar cosine reference
in tests for correctness, without shipping a fallback engine or acceleration
extra. Source installs must not rely on a coincidentally named PyPI project;
the checkout and built wheel are the tested distribution until a release
location is explicitly established.

### Contract tests

Tests use temporary projects and real SQLite. Mock the provider boundary
and inject time, process failure, or placement only where needed.
Test pure value rules and expression construction directly, and compiled
queries against explicit expected results in SQLite. Reserve subprocess
tests for the boundaries that need them: bootstrap, confinement, durability,
limits, and the CLI. Do not require a running daemon for every language test
or assert one exact SQL spelling as a substitute for correct query results.
Organize tests around these observable contracts:

| Contract | Essential cases |
| --- | --- |
| Architecture | Shared contracts import without host dependencies; child bootstrap loads no host graph; cached warming works without process-lifecycle imports; independent evaluators and Kernels do not share mutable session state |
| Project identity | Safe names and paths, exact text preservation, ID resolution, source-version changes, source edited during import, non-destructive metadata publication |
| Session scope | Stable-ID additions/edits/reorders and ID-column renames continue in the same session; deleted IDs count as final orphans and restored IDs recover tags; explicit preservation of generated IDs permits later edits; automatic positional reassignment fails; source/tag name conflicts are reported |
| Replay | Streaming replay agrees with a small sorted reference, including concurrent ties, failed cells, and clears; continuation on a clock behind imported history, valid forked history, rejection of any invalid complete record even with valid later cells, bad headers/numbering/duplicate identities, interrupted headers/tails including partial UTF-8, cached tail warnings, failed-cell/empty-file/tail digests |
| Invalid history isolation | No partial materialization, new applied marker, or stale-cache fallback after validation failure; affected open/export/fork fail without changing original files; info/listing, source rebuilds, source-only operations, and other sessions remain usable |
| Durable completion | Client loss during execution and after log sync, child death before/after result, log append/fsync uncertainty, cache failure after log sync, host death before reply; recover the outcome without executing code twice |
| Private state | Read-your-writes through the same bulk/Entry mutation path, disk-backed tag working tables with bounded memory, newly created fields, failed-cell rollback of tags/counts/FTS and invalidation of Entry/search caches, variables/functions/classes retained on normal failure |
| Concurrency | Two kernels read then tag without a shared snapshot upgrade; embedding waits coexist with another session's commit; exports see committed state; completed and failed cells release read snapshots so idle kernels do not prevent WAL checkpoint progress |
| Language | Method/produce pairs, nested expressions and helper classes, closures/comprehensions and dataclasses across cells, normal Python identity and rejection of bool filters, frozen literal arguments with live field reads, None and predicate negation, numeric/mixed-list comparison, recursive text conversion, container grouping, Unicode operations, standard seed types |
| Search | Isolated field/session BM25, absence/empty/nonmatch, phrase handling, equivalent warm/cold and bounded-batch scores, repeated-query reuse, precise invalidation after writes/rollback, cache eviction without changed answers |
| Embeddings | Full-value requests, Ollama truncation disabled, input ordering, finite packed vectors, dimension races, revision separation, bounded retries |
| Shared warming | Disjoint/balanced shard coverage, row-order-independent assignment, mixed shard-count composition, complete reused/new output, atomic publication, GitHub part sizes, cold-clone use of partial merged packs, whole-pack validation before batched ingestion, interrupted ingestion without a completion receipt, changed-file invalidation, duplicate keys, address independence and revision separation |
| Local lifetime | Separate clients reuse one host/child; concurrent startup and stale endpoints cannot create competing owners; connection errors do not replace live owners; busy exec/reset/close and responsive status during execution/provider waits; reset preserves the live snapshot/configuration; close cleans up before success and never starts a stopped host; host death cleans up its child |
| Runtime and CLI | Inline/file equivalence and info invocation metadata; configured versus applied limits; bounded output; CPU/wall/RSS failure including caught interrupts; fresh-start/replacement and interrupted-append warnings; long Unicode files and complete results; private socket access and long project paths; session validation errors; safe project-relative exports; exit status; actual harness variable persistence |

Run examples from the corrected agent document against a fixture that
supplies their assumed fields and values. Check an explicit public namespace.
Do not parse every inline code span as a required exported name.
Verify that the first-run path works without `info`, that inspection creates
no session, starts no kernel, and omits the manual, and that the packaged
manual matches `USING_QUAIL.md` exactly.
Include absent text/scores in the example fixture. Through separate calls
in the actual harness, reuse classes and variables across inline and file
cells, recover from a normal cell error, wait for a long-running client,
reset, and close. Verify exact multiline Unicode input, complete text/JSON
results, and cleanup of the host, child, endpoint, and locks. Terminate a
client mid-cell and recover its outcome through inspection and logs without
resubmitting it. These are ordinary regression and integration checks using
small temporary fixtures; they do not require a separate probe framework.

### Execution cost and temporary experiments

Keep standard regression tests in the repository, using small temporary
fixtures. Cover avoidable repeated work: unchanged search chains within the
cache budget reuse embeddings and scores; displaying entries does not query
per displayed value; bulk tags do not SELECT per entry; annotation loops
do not rebuild staging tables or rescan the catalog on every write;
unchanged history does not replay; known packs do not decode again. Use
test-side counters where useful, checking reuse and scaling behavior rather
than fixing an exact SQL statement count or a machine-specific latency
threshold. Reattaching a client must preserve the host/child and their
initialized caches, without reopening the dataset or replaying history
for every cell.

Performance benchmarking is ephemeral development work outside the checkout.
Use temporary scripts and generated data when needed to investigate startup,
search, annotation loops, memory use, or concurrent execution, distinguishing
provider latency from local work. Keep benchmark scripts, benchmark-specific
data generators, generated benchmark corpora and vectors, profiling output,
and timing baselines out of the repository and package. They are not part of
what an agent downloads or the committed CI configuration.

Use those measurements to fix execution bottlenecks and retain appropriate
standard regression tests for the behavior corrected. Measure the complete
local path, including serialization, SQLite work, cache publication, and
multiple live kernels, before optimizing a numerical inner loop. Compare
resident and bounded scoring, cold and reused state, and bulk and ordinary
Python annotation. Record corpus dimensions and resource settings alongside
temporary results; a speedup on one fixture is evidence for a choice, not
a universal latency promise. Add further indexes or planner machinery only
for an identified bottleneck.

The initial Core is complete when an agent can import, inspect, search,
annotate, recover from interrupted appends, export, share warming work, and
continue a session from its text project, including source edits with stable
IDs. Build order does not make later slices optional. Automatic salvage of
invalid complete log records, identity remapping, a worker coordinator,
distributed conflict resolution tools, a machine-wide service manager,
extra backend/provider frameworks, and Hosted policy remain outside Core.
The per-session local host in section 8 is the full background-process scope.
