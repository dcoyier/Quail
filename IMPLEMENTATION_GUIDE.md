# Implementation guide

A map from the specification onto source files: **what each file must
contain, and why it lives there.**

This is not a fourth spec. `docs/api.md`, `docs/storage.md`, and
`docs/kernel.md` remain canonical. If this guide and a spec document
disagree, the spec wins. If implementing a slice forces a choice the spec
left open, pin it in the owning spec in **that same PR**, not as folklore
here.

This file is not returned by `quail_setup`. Agents read `api.md`.

---

## Contents

1. [What we are building](#1-what-we-are-building)
2. [What we are not building](#2-what-we-are-not-building)
3. [The two processes](#3-the-two-processes)
4. [Rules that decide where code goes](#4-rules-that-decide-where-code-goes)
5. [What may import what](#5-what-may-import-what)
6. [Suggested order](#6-suggested-order)
7. [The language in one place](#7-the-language-in-one-place)
8. [Repository plumbing](#8-repository-plumbing)
9. [`quail/__init__.py`](#9-quail__init__py)
10. [`quail/project.py`](#10-quailprojectpy)
11. [`quail/index.py`](#11-quailindexpy)
12. [`quail/embed.py`](#12-quailembedpy)
13. [`quail/prelude.py`](#13-quailpreludepy)
14. [`quail/kernel.py`](#14-quailkernelpy)
15. [`quail/tools.py`](#15-quailtoolspy)
16. [`quail/mcp.py`](#16-quailmcppy)
17. [`quail/cli.py`](#17-quailclipy)
18. [`tests/`](#18-tests)
19. [Hosted seams](#19-hosted-seams)
20. [When the spec is silent](#20-when-the-spec-is-silent)

---

## 1. What we are building

Quail is an **environment**, not an agent.

An external agent (Cursor, a CLI harness, later Quail hosted) connects to
a **project** — a directory of ordinary text — and writes Python. Quail
runs that Python against one frozen text corpus and keeps the
conclusions.

The product is five facts. Every file exists to make one of them true.

1. **The corpus does not change.** A dataset is a CSV imported into an
   immutable grid: entries (rows) × fields (columns). An empty CSV cell
   is `None`. Source columns are never overwritten, never typed at
   import (`"00501"` stays `"00501"`).

2. **Conclusions are tags.** A tag is a session-scoped value written onto
   an entry (`tag(billing, "topic", "parking")`). Tags are the only
   analysis state that survives a kernel restart, a new machine, or a
   `git clone`. Variables, functions, classes, and imports are working
   memory. They die with the kernel.

3. **The path to a conclusion is a log.** Every cell appends a JSONL
   record: the code, the output, the tag writes. Replay the logs and
   the tags come back. That is why two agents can work in two sessions,
   push, and merge without a server. Git is the transport. Quail never
   runs git.

4. **The agent writes Python, not a chain of tools.** There is a small
   language: expressions (one value per entry), predicates (true/false
   per entry), and verbs that actually read data (`count`, `retrieve`,
   `values`, `tag`; `fields` reads the catalog). Building a recipe reads
   nothing. A verb compiles it to SQL. `.lexical` and `.semantic` are
   ordinary number expressions — search is how you rank and filter
   (filter = compare the score, e.g. `> 0`), not a separate product.

5. **A cell is a notebook cell that is a transaction for tags.** Prints
   and the last expression behave like Jupyter. Assignments made before
   an error are kept. Tag writes from that cell commit together or not
   at all.

**Core** is this repository: the language, the kernel, the project
format, a stdio MCP server, a CLI. **Hosted** is a separate repository
that wraps core. Core does not know who is calling, where a server is
reachable from, or how people share projects. It exposes seams; hosted
attaches to them.

---

## 2. What we are not building

Easy to confuse with “a persistent Python kernel,” and out of core:

- **Not an agent harness.** Quail does not loop a model, spawn
  subagents, edit the user’s files, or treat chat history as a Python
  variable. The persistent kernel is a notebook on a dataset. Prime
  Agent is the other shape: one IPython tool that *is* the agent, with
  `rlm()`, `bash()`, skills. Do not put those in `prelude.py` or
  `tools.py`. `tools.py` is the host call surface
  (`setup` / `exec` / `export` / `reset`), not an agent tool registry.

- **Not a mutable document store.** There is no update of a CSV cell.
  Source is frozen. The overlay is tags.

- **Not hosted.** No Clerk, no public URL, no multi-tenant policy, no
  operator console, no invitations. Identity and reachability stay out.

- **Not a second dataset in one cell.** One kernel, one session, one
  dataset.

If a proposed function needs the caller’s identity, a network other than
the host’s embedding HTTP, or harness primitives, it does not belong in
core.

---

## 3. The two processes

```text
agent
  │  MCP  (quail mcp)  or  CLI  (quail exec)
  ▼
host process
  mcp.py / cli.py / tools.py / kernel.py / project.py / index.py / embed.py
  owns: project directory, manifest, locks, embedding HTTP, spawn
  spawns one kernel per open session
  ▼
kernel subprocess          python -m quail.prelude
  owns: one session, SQLite connection, the run log, the language
  no network, no filesystem after startup
  ▼
.quail/<dataset>.quail
sessions/<name>/log/<run>.jsonl
```

This picture is `kernel.md` Shape, with the host modules named.

| | Host | Kernel |
| --- | --- | --- |
| Process | `quail mcp` / `quail exec` / a hosted wrapper | `python -m quail.prelude` |
| Network | Yes — embeddings only, via `embed.py` | No |
| Files | Project directory, locks, CSV import | Already-open SQLite + already-open log |
| Language | Does not exist here | The whole language |
| Durable writes | `init`, import, fork, export | Log lines, tag rows, `applied`, `chunks`, `vectors` |

They talk over the kernel’s stdin/stdout as JSON lines. Agent `print`
output is captured **inside** the kernel and returned in `result.output`.
It never rides the control channel as a side stream.

---

## 4. Rules that decide where code goes

These are why the layout is nine modules (`kernel.md` Package layout),
and why `prelude.py` is large on purpose.

1. **The kernel is one file.** A container must run with `prelude.py`,
   `re2`, and optional `numpy`. `prelude.py` is a flat module
   (`quail/prelude.py`), not a package folder, and it imports nothing
   else from `quail`. After startup the process has no package, no
   network, and no filesystem — only this module and two C-level
   handles (SQLite, the log). Therefore the language, the compiler, the
   verbs, the cell loop, replay-into-SQLite, and the sandbox all live in
   `prelude.py`.

2. **The host has the network; the kernel does not.** Embedding HTTP
   belongs in `embed.py`. The kernel sends `{"op": "embed", ...}` and
   blocks. It never imports `embed.py`.

3. **MCP is a wire, not the product.** `tools.py` is the real host API.
   `mcp.py` is a stdio MCP adapter. `cli.py` is an argv adapter. Hosted
   will call `tools.py` over HTTP. Logic that lands in `mcp.py` cannot
   be reused.

4. **The directory is the truth; SQLite is a cache.** Parsing
   `quail.toml`, session folders, and JSONL belongs in `project.py`.
   Creating and rebuilding `.quail/<dataset>.quail` belongs in
   `index.py`. Deleting the index must always be safe.

5. **Locks are taken by the host, before spawn.** `project.py` knows the
   path and how to `flock`. `kernel.py` holds the lock for the life of
   the subprocess. The kernel process itself does not lock.

6. **Two `QuailError` classes, one JSON shape.** Host failures
   (bad manifest, unknown dataset, lock) live next to `Project`. Kernel
   failures live in `prelude.py`. The wire is `{type, message, hint}`.
   `tools.py` translates kernel JSON into the host exception only when
   the **tool** failed. Cell-level errors stay in the `exec` result
   dict. Do not add `quail/errors.py` so both processes can import it —
   prelude cannot import `quail.*`.

7. **Replay is a spec, not a shared module.** Last-write-wins is
   `storage.md`. `project.py` parses logs into a map. `prelude.py`
   applies that map to SQLite **on kernel open** (this is how a
   `git pull` takes effect). `index.py` applies it **only when
   re-importing a CSV**. Do not add `replay.py`. Do not invent a host
   path that applies tags with no kernel for a pull; the spec’s on-open
   rule is the pull path.

   `kernel.md`’s package line currently says `project.py` owns “replay”.
   Read that as parse / digest / the pure map. SQLite apply on open is
   prelude. Tighten the one-liner in `kernel.md` in the PR that
   implements it.

8. **Prefer making a mistake unrepresentable.** Illegal `where=` types,
   non-number `rank=`, `is None`, tagging a source field — fail because
   the objects cannot be formed or the verb will not accept them, not
   because `api.md` grew a caveat.

9. **`quail init` / `quail import` write the project’s `quail.toml`.**
   That is the project manifest (like `pyproject.toml`). It is not the
   old operator file.

---

## 5. What may import what

```text
cli.py ──► tools.py ──► kernel.py ──spawns──► prelude.py   [other process]
              │              │
              ├──────────────┼──► project.py
              └──────────────┴──► index.py

kernel.py ──► embed.py ──► project.py      HTTP, host only

mcp.py ──► tools.py, project.py
cli.py ──► mcp.py                          only to hand off `quail mcp`
cli.py ──► project.py, index.py

prelude.py ──► stdlib, re2, optional numpy
               NOTHING in quail.*
```

| Module | May import | Must not import |
| --- | --- | --- |
| `project.py` | stdlib | `index`, `prelude`, `embed`, `kernel`, `mcp` |
| `index.py` | `project.py`, stdlib, sqlite3 | `embed`, `prelude`, `mcp` |
| `embed.py` | `project.py`, stdlib, HTTP | `prelude`, `index` |
| `prelude.py` | stdlib, `re2`, optional `numpy` | any `quail.*` |
| `kernel.py` | `project.py`, `embed.py`, stdlib | `prelude` as a library (it **spawns** it); `index` |
| `tools.py` | `project.py`, `index.py`, `kernel.py` | `embed`, `mcp`, `prelude` |
| `mcp.py` | `tools.py`, `project.py`, MCP SDK | language or SQL |
| `cli.py` | `tools.py`, `project.py`, `index.py`, `mcp.py` | `embed`, `prelude` |
| `__init__.py` | host names from `project` / later `kernel` / `tools` | prelude types (other process) |

**Rebuild the index before spawn.** The kernel cannot read the CSV after
startup. `tools.setup`, `tools.exec`, and `Kernel()` (or whatever host
path opens a dataset) must call `index.py` **before** spawn when the
index file is **missing** (clone, deleted `.quail/`) **or** the CSV hash
differs from `meta.source_hash`. Pin that call site in `kernel.md` when
you implement it. `storage.md`: a second machine clones the project and
Quail rebuilds the index from what it finds.

**`quail warm`** does not let `index.py` call `embed.py`. Warm means
“run the first `.semantic()` now”: `tools.py` → `kernel.py` → a cell
(or a small control op you add to `kernel.md` in that PR). Prelude
writes `chunks` and `vectors` on that connection.

Two kernels warming the same field at once both embed and both
`INSERT OR IGNORE` (`kernel.md` Locks). The work is duplicated; the
result is not. That lives in prelude’s vector insert, not in `embed.py`.

---

## 6. Suggested order

Do not create a file before its step. Do not stub verbs.

| Step | Slice | Why this next |
| --- | --- | --- |
| 1 | `pyproject.toml`, empty `quail/__init__.py` | The package exists. Re-exports wait until `Project` exists. |
| 2 | `project.py`: manifest + paths + host `QuailError` | Hosted’s first seam is `Project(path)`. |
| 3 | `project.py`: `session.toml`, log parse, digest, last-write-wins map, fork, flock helpers | Durable truth, still no SQLite. |
| 4 | `index.py`: import CSV, schema, FTS; re-import reapplies tags from the map | There is a grid to query. |
| 5 | `prelude.py`: types, pipeline produce, compiler, `q_` functions, verbs against that index | The language is real. One slice — not a fake `count`. |
| 6 | `prelude.py`: cell loop, log append, sandbox | One cell is the product. |
| 7 | `kernel.py`: spawn, JSON lines, wall timer, restart, `flock` | The host can run a cell. |
| 8 | `embed.py` + `embed` / `embedded` | `.semantic()` can leave the box. |
| 9 | `tools.py` | The four functions hosted will wrap. Hash-check lives here. |
| 10 | `mcp.py` and `cli.py` | Surfaces last, so they cannot grow logic. |

---

## 7. The language in one place

This section is the type system `prelude.py` must implement. The method
table, absence rules, and search semantics stay in `api.md`; the SQL
stays in `storage.md`. This is the *shape* so verbs do not become special
cases.

### Two classes, not three

| Class | Meaning |
| --- | --- |
| `Expression` | A recipe for one value per entry. `Field(name)` is the simplest. Nothing is read until a verb or `entry[...]`. |
| `Predicate` | True or false per entry. Not an expression. |

`number` is **not** a class. It is what an expression **produces**. In
this guide, “number expression” always means an `Expression` whose
pipeline produce is `number`. There is no `Number` type and no
`NumberExpression` class.

| Origin | `FieldInfo.kind` | produce |
| --- | --- | --- |
| source column | `"source"` | `text` |
| tag | `"tag"` | `any` |

`api.md`’s method table “Produces” column is `text`, `number`, `list`,
`any`, or `predicate`. Call the first four **pipeline produce** inside
the compiler. Do **not** call them `kind` in any public name:
`FieldInfo.kind` is already `"source"` or `"tag"`. Do not put a public
`.kind` on `Expression` unless `api.md` grows one (the name-agreement
test would then require it). Keep produce on the tree internally.

Source `Field(name)` produces `text`. Tag `Field(name)` produces `any`.
Unknown names raise at construction and list the fields.

Every pipeline method returns another `Expression`, **except** `.isin`
and `.contains`, which return a `Predicate`. `api.md` still says every
method returns an `Expression`; the table says those two produce
`predicate`. In the same PR that implements types, make the sentence
match the table. Until then, implement the table: `predicate` in
Produces means class `Predicate`, not an `Expression` with produce
`predicate`.

`Predicate` is produced by comparison (`==` `!=` `<` `<=` `>` `>=`), by
`.isin` / `.contains`, and by `&` `|` `~`. Python `and` / `or` / `not`
raise. Both classes have no truth value: `__bool__` raises, so
`if pred:`, `pred and other`, `0 < expr < 10`, and `x in Field("f")`
fail. Membership is `.isin` / `.contains`.

`is` / `is not` are **rejected** before the cell runs (`QuailError`:
`` `is` is not allowed; write `== None` ``). They are not rewritten to
`==`. Rewriting would make `x is y` silently compare by value.

### Which slot takes which class

Bind slots to **verbs**. A shared kwargs bag will not match `api.md`.

| Slot | Verb | Accepts |
| --- | --- | --- |
| `where=` | `count`, `retrieve`, `values` | `Predicate`, or omitted (all rows) |
| first positional | `count`, `retrieve` | `where` |
| `by=` | `count` only | `Expression` or `list[Expression]` (a list → tuple keys, a cross-tab) |
| `rank=` | `retrieve`, `values` | `Expression` (produce `number`) |
| first positional | `values` | the `expr` to materialize (an `Expression`) |
| `tag(target, field, value)` | `tag` | target: `Predicate`, `Entry`, or `list[Entry]` — **not** `where=` |
| `tag` value | `tag` | `bool`, `int`, `float`, `str`, `list`, `dict`, `None` (clear), or `Expression` |
| `entry[expr]` | | any `Expression` |

Traps the signatures already imply:

| Call | Binds as | Legal? |
| --- | --- | --- |
| `count(long)` | `where=long` | yes, if `long` is a `Predicate` |
| `count(Field("topic"))` | `where=` an `Expression` | no — grouping is `count(by=Field("topic"))` |
| `retrieve(score)` | `where=score` | no — ranking is `retrieve(rank=score)` |
| `retrieve(long, limit=5)` | `where=long` | yes |
| `retrieve(where=score)` | number as filter | no — write `score > 0.5` |
| `count(Field("body").lexical("q"))` | number as `where` | no — write `> 0` |
| `values(long)` | `expr=long` | no if `long` is a `Predicate` |
| `tag(Field("body"), "f", 1)` | `Expression` as target | no |
| `count(retrieve(...))` | `list[Entry]` as `where` | no — `Entry` lists are `tag` targets |

Make those call-time errors. Do not add agent-facing caveats unless the
signature still cannot show it.

`rank=` has no alphabetical sort. Without `rank`, order is import order
(`e.rowid`). Unary `-` on a number expression sorts ascending. Always
`ORDER BY <rank> DESC NULLS LAST, e.rowid` so `None` stays last even
when ranking by `-length`.

`by=` is a tally: walk matching rows, look at the expression’s value,
increment that bucket. Result is a `collections.Counter` (a map
value → count), most common first. Two different lists: a Python
`list[Expression]` passed to `by=` makes tuple keys (a cross-tab). If
the *by-expression’s produce* is `list`, one row increments once per
item. Absent values bucket under `None`.

### Absence, arithmetic, search

Absence is `None` and flows: every method maps `None` → `None`.
Comparisons with `None` are false except `== None` / `!= None`. `None`
sorts last under `rank`. `count(by=)` groups it under `None`.

Arithmetic (`+ - * /` and unary `-`) is only on number expressions and
numeric literals. Result is a number expression. `/` is real division
(`CAST AS REAL`); `/ 0` is `None`. Comparing a text/any expression to a
**numeric literal** coerces the left with `q_number` (`Field("age") > 30`).
Comparing two expressions does **not** coerce. Do not apply the
comparison coerce to `+`.

`.lexical` / `.semantic` produce `number`. They are legal in `rank=` and
in `+`. They are not `where=` until compared (`lexical("q") > 0`).
Lexical: `0` means no query word appeared, `> 0` means matched; absent
cell is `None`/`NULL`; those scores are relative to the whole dataset
and are not comparable across datasets or fields (`api.md` `.lexical`).
Semantic: no default threshold; long cells score by best passage;
`query` is text (`semantic(e["body"])` is just a string). First
`.semantic()` on a field embeds every cell once and the **cell reports
what it did**; later calls are fast; tag fields work the same. Lexical
and semantic live on different scales; when you sum them, choose
weights by reading results (`api.md`). Without `embed` in the manifest,
`.semantic()` raises with a hint to add it (`storage.md`).

Regex is RE2. Flags: `re.I`, `re.M`, `re.S` from the pre-imported `re`.

`Random(seed=None)` is a constructor (not a method on `Field`) that
produces `number`. Seeded: stable per `(seed, rowid)`. Unseeded: SQLite
`random()`. Used as `rank=` to sample.

Pipeline illegal combinations raise **when the expression is built**,
naming both sides. Implement `api.md`’s method table as the single
produce-check. `.sub` / `.slice` produce `same` (input produce; `any` →
the table’s `text_or_list` equivalent as written).

---

## 8. Repository plumbing

### `pyproject.toml`

**Why.** The package has to be installable; the CLI needs an entry
point. Dependencies land with the modules that need them, not all at
once.

**Contains.**

- `requires-python = ">=3.12"`
- name `quail`
- Apache-2.0, matching `LICENSE`
- console script `quail = quail.cli:main` when `cli.py` exists
- dependencies as needed: `google-re2`, `mcp`; optional `numpy`
- `pytest` as a dev extra
- package data for `docs/api.md` when `setup` must return it. Pin the
  resolution in `kernel.md` in that PR. Do not read it from the
  developer’s cwd.

**Does not contain.** Dataset settings, provider URLs, kernel limits.
Those live in the **project’s** `quail.toml`. No `setup.py`, no `src/`
layout, unless a later PR has a concrete reason.

### `.gitignore`

Already correct on this branch:

- Python: `__pycache__/`, `.venv/`, `.pytest_cache/`, `dist/`
- Derived project files: `.quail/`, `sessions/*/.lock`

Do not gitignore `sessions/` or `data/`. Those are the truth.

### `docs/api.md`, `docs/storage.md`, `docs/kernel.md`

Do not re-home these. Agent-facing wording → `api.md`. On-disk and SQL
→ `storage.md`. Process → `kernel.md`. Same PR if they disagree.
`api.md` is returned verbatim, so every sentence costs context.

### `LICENSE`

Unchanged. Apache-2.0.

---

## 9. `quail/__init__.py`

**Job.** Make `import quail` useful on the **host**.

**Why.** The kernel process is not this interpreter. Host callers
(`from quail import Project`) must not pull MCP or spawn a kernel.

**Contains.** Re-exports of the host seam once those names exist:
`Project`, host `QuailError`, later `Kernel` and the `tools` functions
if that stays small.

**Does not contain.** `Field`, `Expression`, `Predicate`, verbs. Those
are prelude names. They exist inside a running kernel.

**Tests.** `import quail` does not import `prelude`.

Step 1 ships this file **empty** (or a one-line docstring). Re-exports
are a one-line change after step 2.

---

## 10. `quail/project.py`

**Job.** The project directory as data. Hosted’s first seam:
`Project(path)`.

**Why.** CLI, import, fork, lock, and setup all need the same paths and
the same log parser. None of that is SQL and none of it is the language.

`kernel.md` lists this file as “manifest, paths, sessions, logs, replay,
locks”. Host-side ownership of those concepts. The kernel also replays
into SQLite inside `prelude.py`, because it cannot import this module.
Shared rules live in `storage.md`.

### Manifest (`quail.toml`)

Load and validate. Unknown keys are an error at every table of this
file (`storage.md`).

- `[project] quail = "1"` is the schema version. Whether other versions
  fail is silent; pin in `storage.md` when you implement.
- `[datasets.<name>]`: `source` required (path relative to the
  manifest, resolved to absolute for callers), `id` optional, `embed`
  optional (`"provider/model"`).
- `[providers.<name>]`: `base_url`, `api_key`. `api_key` is only
  `env:NAME` — never a literal. Store the variable name; do not read
  the env here. `embed.py` reads it at call time. Default URLs when the
  `ollama` / `openai` tables exist without `base_url`, matching
  `storage.md`.
- `[kernel]`: `cpu_seconds`, `wall_seconds`, `memory_mb`, `max_limit`,
  `output_kib`. Omitted keys take the defaults in `storage.md` (30,
  120, 1024, 1000, 64).

`Project(path)` takes a **directory** (`kernel.md` hosted seam). Accepting
a `quail.toml` file path is an implementation convenience, not the seam.

### Layout helpers

Do not create directories unless a write operation needs them.

- `root`, `manifest_path`
- `sessions_dir`, `exports_dir`, `index_dir` (`.quail/`)
- `index_path(dataset)` → `.quail/<dataset>.quail`
- `dataset_source(name)` → resolved CSV
- `session_dir(name)`, `session_manifest_path(name)`,
  `session_log_dir(name)`, `session_lock_path(name)`

### Sessions

- Read/write `session.toml`: `dataset`, `created`, optional
  `forked_from`, optional `description`. Unknown keys: spec is silent
  (the unknown-key rule is written for `quail.toml`); pin in
  `storage.md` if you reject them.
- Create a session directory (`tools.exec` when the name is new;
  `quail fork`).
- Fork: copy `log/` byte-for-byte (keep run file names), write new
  `session.toml` with `forked_from`. Host filesystem copy, not a kernel
  operation.

### Logs — parse, do not append

The kernel holds the append handle. This module **reads**:

- List `log/*.jsonl`
- Parse lines; skip broken lines and report them
- Distinguish run header from cell records
- Views for `quail_setup` / `quail sessions`: cell count, last active,
  tag field names. The payload field `orphan_tags` is counted in
  `index.py` (it has `entries`): ids present in the last-write-wins map
  that are missing from `entries`. `project.py` does not invent public
  methods named `cells()` / `tag_writes()`; the JSON fields are the
  contract.

Cell record fields (`storage.md`): `n` (from 1 within the run),
`started` / `ended` (RFC 3339 UTC), `code`, `output` (after truncation),
`error` (`null` or `{type, message, hint}`), `tags` (list of
`{field, values: {entry_id: value_or_null}}`). Entry ids, never row
numbers. Failed cells are logged and have `tags: []`.

Run header: `run`, `started`, `actor`, `quail`, `dataset`,
`source_hash`. `actor` from `QUAIL_ACTOR`, default hostname. Provenance,
not identity.

**Digest.** A deterministic digest over the session’s log files. Prelude
uses the same algorithm. Pin it in `storage.md` in the PR that
introduces it. If they disagree, every open replays.

**Last-write-wins map.** Given successful cell records
(`error == null`), sort by `(started, run, n)`. For each
`(entry_id, field)`, last write wins; `null` deletes. Returns a map, not
SQLite rows. `index.py` and `prelude.py` each apply that map. If the
two appliers drift, the spec still wins: copy the rules, do not import.
A comment in both places pointing at `storage.md` Replay is the
coupling.

### Locks

- Path: `sessions/<name>/.lock`
- Advisory `flock` while a kernel is open
- Failure names the session and the holder
- File is gitignored; creating it is fine
- This module exposes acquire/release. `kernel.py` is the only
  long-term holder.

### Host `QuailError`

`QuailError(message, hint=None)`. Same shape the agent sees. Kernel has
its own class in `prelude.py`. Same name, two processes.

### Must not contain

SQLite, HTTP, expression compilation, MCP types, appending log lines,
git commands.

### Tests

Unknown keys at every `quail.toml` table; `api_key` literal rejected;
`env:NAME` stored as the variable name; kernel defaults when `[kernel]`
omitted or partial; `source` resolved relative to the manifest; fork
copies logs and sets `forked_from`; log parser ignores bad lines;
last-write-wins + `null` deletes; second `flock` fails with the session
name.

**Spec.** `storage.md` (layout, manifest, sessions, log, replay, locks);
`kernel.md` (`Project(path)`, lock-before-spawn).

---

## 11. `quail/index.py`

**Job.** CSV → `.quail/<dataset>.quail`, and rebuild when the CSV
changes.

**Why.** The index is derived and disposable. Import is a host operation
(it reads a CSV). The kernel only opens a file that already exists.
Mixing import into `prelude.py` would pull `csv` and DDL into the
sandbox file.

### Schema

Implement `storage.md` The index **exactly**:

- `meta` (schema_version, source_hash, id_column, columns JSON,
  embed_model, embed_dims, imported_at)
- `entries` (rowid PK, unique `id`, one quoted column per CSV header)
- `entries_fts`: FTS5, `tokenize='porter unicode61'`, every source
  column **except** `id`, `content='entries'`, `content_rowid='rowid'`
- `tags` (session, entry, field, value JSON text;
  PK `(session, entry, field)`)
- **index** `tags_by_field` on `tags(session, field)` — not a table
- `tags_fts`: FTS5, `tokenize='porter unicode61'`, `value` plus
  UNINDEXED session/field/entry
- `vectors` (model, text_hash, float32 little-endian blob;
  PK `(model, text_hash)`)
- `chunks` (session, field, entry, n, text_hash);
  source rows use session `''`; tag rows use the session name
- `applied` (session PK, log_digest)

WAL mode. `busy_timeout` of five seconds is **per connection**. Prelude
must set it on open too, not only this module. Deleting the file is
always safe.

`entries` is written once per import and never updated. FTS is built
with the `rebuild` command. The kernel authorizer denies writes to
`entries` and `entries_fts`.

Tag values are JSON text: `"parking"`, `3`, `true`, `["a","b"]`. Reading
decodes.

### Import

- UTF-8 CSV, header row
- Every cell is text. Empty → SQL `NULL`. No type inference.
- `id` column unique and non-empty. If neither the manifest nor the CSV
  has one, synthesize `row-000001` in file order and **warn**: tags key
  by id; a later re-import without stable ids orphans tags.
- Record content hash in `meta`.

### Re-import

When the CSV’s content hash differs from `meta.source_hash`:

- Rebuild `entries`, `entries_fts`, and **source** `chunks`
- Keep `vectors` (content-addressed; unchanged text costs nothing)
- Re-derive every session’s tags from `project.py`’s log map
- Tags whose entry id no longer exists stay in the log; `index.py`
  counts `orphan_tags` as last-write-wins ids missing from `entries`
  (not raw write count)

Live `tag()` in a cell still writes `tags` **only in prelude**.
Re-import writing `tags` is rebuilding a cache, which `storage.md`
Datasets requires. It is the exception to “nothing else writes `tags`”
on the live path.

**Source `chunks`.** Rebuilding them **is** passage splitting + hashing
(~1500 characters on paragraph boundaries — `storage.md` Semantic).
Prelude does the same and cannot import this module. Pin in `storage.md`
in the import PR: either both files follow that section (duplicated
split rules, comments pointing at the spec), or re-import **deletes**
source chunks and the next `.semantic()` rebuilds them. Do not add
`chunk.py`.

Do not invent a requirement to delete `applied` unless you also rewrite
it. Re-import does not change the logs: an untouched digest still
**matches**, so the next open will **not** replay. If you delete
`applied` and do not rewrite it, the next open always replays. Pin the
choice in `storage.md`.

### Must not contain

Embedding HTTP, expression trees, MCP, writing `quail.toml`, a
no-kernel git-pull apply, calling `embed.py`.

### Tests

Empty cells are `NULL` not `""`; `"00501"` stays text; duplicate/empty
ids fail; synthesized ids + warning; re-import keeps vectors and
reapplies tags; orphans counted after last-write-wins; FTS on source
columns except `id`.

**Spec.** `storage.md` Datasets, The index; `kernel.md` authorizer names.

---

## 12. `quail/embed.py`

**Job.** Turn texts into vectors on the host.

**Why.** The kernel has no network. Hosted replaces this module by
answering `embed` requests itself.

**Contains.**

- Read `base_url` and `api_key` env name from `Project`
- Batch and retry (`kernel.md` Services). Do not invent a timeout
  number the spec did not give; if you need one, pin it in `kernel.md`
- Return `list[list[float]]` aligned with the input
- Map provider failures to `{"op": "embedded", "error": {...}}`

Dimension is learned from the first response and recorded in the index
(`storage.md`). First `.semantic()` is prelude; prelude writes
`vectors` / `meta` as the authorizer allows. The allow-list in
`kernel.md` does not currently include `meta` — pin `embed_dims` writes
in `kernel.md` / `storage.md` when you implement (authorizer vs `meta`).

**Must not.** Passage splitting, hashing, `vectors` table writes, MCP.

Tests for HTTP batching live with `test_kernel.py` (embed forwarding).
Prelude semantic tests mock the control channel; they do not import this
module.

**Spec.** `storage.md` Manifest providers and Semantic; `kernel.md`
Services.

---

## 13. `quail/prelude.py`

**Job.** The kernel. Language, compiler, verbs, cell loop,
replay-into-SQLite, sandbox.

**Why.** Isolation (rule 1). The agent never reads this file. `api.md`
is the documentation of its public names. A test asserts agreement
**both ways**: every name `api.md` uses in code exists here; every
public name this module puts in the user namespace appears in `api.md`.

### 13.1 Startup — nine steps, in order

Implement `kernel.md` Cell 0 as written. Do not add a tenth step or a
handshake after `ready`.

1. Parse arguments: project root, dataset, session, run id, limits.
   How they arrive (argv vs spawn-payload) is silent; they must arrive
   **before** opening the DB, not as a control message after `ready`.
   Pin the chosen channel in `kernel.md`.
2. Open `.quail/<dataset>.quail`. Set `busy_timeout` 5s, WAL already on
   the file. Register `q_` functions. Install the authorizer: deny
   `INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER` on `entries` and
   `entries_fts`; allow those on `tags`, `tags_fts`, `chunks`,
   `vectors`, `applied`, and temp tables.
3. Digest this session’s log files. If digest ≠ `applied.log_digest`,
   delete this session’s rows from `tags`, `tags_fts`, and `chunks`,
   replay (`storage.md`), **record the new digest**. Cache the field
   catalog: source columns from `meta`, tag fields from `tags`.
4. Open `sessions/<name>/log/<run-id>.jsonl` for append. Write the run
   header (`storage.md` fields). Keep the handle; every later write goes
   through it. Connection and log handle live in a **closure**, not in
   the user namespace. Introspection can still find them; core accepts
   that.
5. Import `re`, `math`, `statistics`, `json`, `itertools`,
   `collections`, `Counter`. Build the namespace (below).
6. `RLIMIT_AS` from `kernel.memory_mb`.
7. Audit hook. Deny the events listed in `kernel.md` Cell 0 step 7
   (copy from there; do not summarize a shorter list). Hooks cannot be
   removed once added.
8. Linux: `os.unshare(CLONE_NEWUSER | CLONE_NEWNET)` if possible; if
   not, continue and say so in the run header.
9. Report `ready` on the control channel. Enter the cell loop.

Steps 6–8 are the whole sandbox. There is no allow-list of Python
constructs. Ordinary `import csv` is fine; it still cannot `open` a
file. Do not wrap filesystem calls in prelude helpers that “just this
once” open something — the hook denies `open`.

Everything the kernel needs later is already open by step 4. SQLite I/O
is C-level and raises no Python audit events, so the connection keeps
working after step 7.

### 13.2 User namespace

Injected, and also attached to a `quail` object so
`count = quail.count` recovers a shadow (`kernel.md` Names puts every
public name on `quail`, including `Field`):

| Name | Role |
| --- | --- |
| `Field` | Constructor. `Field(name)` → simplest `Expression`. |
| `Random` | Constructor. `Random(seed=None)` → number expression. |
| `Expression` | Type. Not a constructor the agent is shown. |
| `Predicate` | Type. Same. |
| `Entry` | Type `retrieve` returns. |
| `FieldInfo` | `fields()` items: `name`, `kind` (`"source"` or `"tag"`), `present`. |
| `QuailError` | `message` + optional `hint`. |
| `count` `retrieve` `values` `tag` `fields` | Verbs. |
| `quail` | Same names as attributes. |
| already imported | `re`, `math`, `statistics`, `json`, `itertools`, `collections`, `Counter` |

`print` is ordinary Python. Do not wrap it; redirect stdout/stderr
during the cell.

`kernel.md` step 5 lists verbs, `Field`, `Random`, `QuailError`, `quail`.
`api.md` also names `Expression`, `Predicate`, `Entry`, `FieldInfo`.
Include them so the bidirectional test and `isinstance` work.

### 13.3 Types and verbs

Section 7 is the type system. Implement `api.md` Verbs in full:

**`count(where=None, by=None) -> int | Counter`**

- No `by`: `SELECT count(*) … WHERE <pred>`. Omitted `where` = all
  rows.
- With `by`: fetch the by-columns, flatten list cells, `Counter`, most
  common first.

**`retrieve(where=None, rank=None, limit=10, offset=0) -> list[Entry]`**

- `rank` omitted: import order. Present: number produce; highest
  first; `None` last; unary `-` for ascending.
- `limit` default 10, **clamped** to `max_limit` with a note in the
  output (not a raise). `values` is **not** clamped (`kernel.md`).
- `offset` pages.
- Then one `SELECT` for source cells of those rowids and one for their
  tags (`storage.md` Verbs). Fill `e.score` from the rank column when
  `rank=` was used, else `None`.

**`values(expr, where=None, rank=None, limit=None) -> list`**

- One computed value per matching entry; rank order if `rank` given,
  else import order. `limit=None` means all. Prefer this over
  `entry[expr]` in a loop (one query per entry).

**`tag(target, field, value) -> int`**

- Target: predicate → `WHERE`; `Entry` / `list[Entry]` → those ids.
  Empty list: return `0` (nothing written).
- `field`: create on first write; **exists while at least one entry
  still carries it** (clearing the last value drops it from
  `fields()`); reject source names.
- `value`: `bool`, `int`, `float`, `str`, `list`, or `dict`, `None` to
  clear, or `Expression` per entry. Tuples and sets are not in the
  list. Whether `NaN` is a legal `float` is silent; if you reject it,
  pin JSON-like in `storage.md` / `api.md` in that PR.
- `INSERT OR REPLACE`; `None` / computed `NULL` deletes. Update
  `tags_fts`. Delete `chunks` for touched rows so the next
  `.semantic()` re-chunks.
- Accumulate `{entry_id: value}` per field for the log, **in the order
  performed**; two writes to the same `(entry, field)` in one cell
  collapse to the last. Literal `None` or computed NULL recorded as
  `null`.
- Return how many entries were written. Replace, never append.
  Multi-label: boolean fields or read/extend/rewrite a list (`api.md`).

**`fields() -> list[FieldInfo]`**

- `name`, `kind` (`"source"` or `"tag"`), `present` (non-`None` count).
- Order is unspecified in `api.md`. Pick one and, if it matters to
  agents, pin it in `api.md` in that PR. Do not treat “source then
  tags” as already specified.

**`Entry`:** read-only mapping, source + this session’s tags. `e.id` is
`e["id"]`. `e[expr]` compiles `expr` for this rowid. `e.score` as above.
`dict(e)`, `.items()`, `"topic" in e`. `repr` shortens long text and
notes the full length.

### 13.4 Compiler

Walk the tree. Emit SQL for what SQLite does well. Call `q_*` for the
rest. The tables in `storage.md` (Joins, Expressions, Predicates, Verbs,
Registered functions, Lexical) are the checklist. Encode them in code
(op → SQL) so tests can compare; do not maintain a third copy here.

Invariants worth repeating because they are easy to get wrong:

- One verb → the SQL shape in `storage.md` Verbs (`tag` is SELECT then
  INSERT OR REPLACE, plus FTS/`chunks`)
- `entries` aliased `e`
- `LEFT JOIN` per referenced tag field, lexical query, semantic query
- Predicates: `(x op y) IS TRUE` so SQL UNKNOWN becomes false
- `== None` / `!= None` → `IS NULL` / `IS NOT NULL`
- Lexical queries **sanitized** into FTS5, never passed through: each
  whitespace-separated word becomes a quoted token; a double-quoted
  span becomes one phrase token; words joined with `OR`; restricted to
  the field’s column (`"body" : …`). Stemming is porter. No other
  operators. `bm25()` is smaller-is-better; the join **negates** it.
  Absent cell `NULL`; no-match `0`.
- `.semantic` score from `temp.sem_k`; missing cell `NULL`
- `q_number`, `q_text` (arrays joined with `"\n"`), `q_length`,
  `q_search` / `q_findall` / `q_sub` / `q_slice`, `q_contains`,
  `q_random` — all deterministic, NULL-preserving

**Semantic on this connection** (`storage.md` Semantic):

- Split ~1500 characters on paragraph boundaries
- Hash passages; missing hashes → `embed` on the control channel;
  store `vectors`; map in `chunks`
- Queries hashed and cached the same way
- Load passage matrix once per field per kernel; cosine; max per
  entry; write `temp.sem_k`
- numpy if present, else `math.sumprod` over `array('f')`
- Approximate indexes out of scope; ~100k entries is the honesty note
  in `storage.md`, not a hard cap to implement

While `embed` is in flight the host pauses the **wall** clock. The
kernel CPU timer does not tick (the process is blocked on stdin).
Provider error → `QuailError` with a hint to check the provider.

### 13.5 Cell contract

Implement `kernel.md` The cell contract in order:

1. `ast.parse`. Any `Is` / `IsNot` → `QuailError` before execution,
   exact message in `kernel.md`. Other `SyntaxError` as itself.
2. `BEGIN`
3. Arm CPU timer (`setitimer(ITIMER_VIRTUAL, cpu_seconds)`). Redirect
   stdout/stderr into a buffer.
4. Execute every statement but the last. If the last is an expression,
   evaluate it and, when not `None`, append its `repr` (notebook
   display). Prints already landed in the buffer.
5. Success: cell record with accumulated tag writes, append log,
   **fsync**, `COMMIT`, update `applied.log_digest`, refresh catalog.
6. Exception: `ROLLBACK`, discard accumulated tags, append cell record
   with `error` set and `tags: []`, traceback in the buffer with
   prelude frames stripped. Spec does not require fsync on this path;
   do not pretend it does.
7. Disarm timer; restore streams; truncate to `output_kib`. The note
   format is `kernel.md`
   (`\n[quail] output truncated at 64 KiB (of 1.2 MiB)` in the example):
   the first size is `output_kib`; the parenthetical is how large the
   buffer was **before** truncation, not a second configured limit.
   Output is never discarded, only truncated. Respond.

Commit order is load-bearing (`storage.md` On commit): **log line +
fsync, then SQLite commit, then `applied.log_digest`**. Die between
them → next open sees a digest mismatch and replays. Do not `UPDATE
applied` inside the tag transaction.

Namespace is **not** rolled back. Tags from this cell are.

Control I/O (`kernel.md`):

```json
{"op": "run", "code": "…"}
{"op": "result", "n": 12, "output": "…", "error": null, "tags_written": 0, "truncated": false}
```

`error` is `null` or `{type, message, hint}`. Non-Quail exceptions:
Python type name, message, `hint` null.

CPU `SIGVTALRM` → `QuailError` in the main thread (rolls back, kernel
lives). `MemoryError` the same. Wall clock is the **host**: `SIGINT`
(→ `KeyboardInterrupt`); `SIGKILL` 5 seconds later.

### 13.6 Must not contain

HTTP; `import quail.*`; MCP; creating datasets / writing `quail.toml`;
taking the session flock; `rlm()`, file editors, subagents, `bash()`.

### Tests that belong here

- Name agreement with `api.md` (bidirectional)
- Produce-check: every illegal pipeline in the table raises at build
- `__bool__` on Expression and Predicate raises
- `is` rejected at AST (not rewritten)
- `where=Field("body")` rejected; `rank=Field("topic")` rejected;
  `tag(Field("body"), …)` rejected
- `count(by=)` returns a `Counter`; list cells flatten
- retrieve clamp note; `offset`; `e.score`; values uncapped and
  rank/import order
- tag rollback on exception; names assigned before the error remain;
  last write wins inside the cell; field disappears when cleared
- log fsync ordering via a crash fixture if you can observe replay
- authorizer: writing `entries` fails
- audit hook: `open(...)` fails after startup
- lexical: quoted tokens, phrases, `OR`, `-bm25`, 0 vs NULL
- semantic: mock the control channel; cache hit does not re-embed;
  cell reports first-time embed

**Spec.** All of `api.md`; `kernel.md` Cell 0, cell contract, names,
limits; `storage.md` compile + sync rules.

---

## 14. `quail/kernel.py`

**Job.** Spawn a prelude process, own the control channel, enforce wall
time, restart.

**Why.** The host must not evaluate agent code.

**Contains.**

- `Kernel(project, session, spawn=…)` — hosted seam. Default `spawn` is
  a local subprocess. Hosted substitutes a container/microVM spawn that
  still speaks the same JSON lines, with the project mounted.
- Take `flock` (via `project.py` helpers) before spawn; hold until the
  kernel dies.
- Pass project root, dataset, session, run id, limits (the channel
  pinned with prelude startup).
- Wait for `ready`.
- `run(code) -> result dict`.
- Forward `embed` to `embed.py` or to a callback hosted injects; pause
  the wall timer while waiting.
- Wall timer: `SIGINT` at `wall_seconds`; `SIGKILL` 5 seconds later.
- On death: next `run` respawns. If the digest matches, prelude will
  not replay. Set `kernel_restarted: true` on the next **tool** result
  (not on the kernel `result` op).
- Serialize cells for this session: a second `exec` waits, it does not
  fail (`kernel.md` Locks). A second **process** trying the same
  session fails the flock.

Run ids: `<UTC timestamp>-<6 hex>`, e.g. `20260901T210000Z-a1b2c3`. One
process, one log file, append only.

**Must not.** SQL, the language, MCP schema, opening the CSV,
`import index`.

Tests: `test_kernel.py` (spawn, restart flag, wall interrupt, embed
forwarding).

**Spec.** `kernel.md` Shape, Services, Limits and recovery, Locks,
hosted `Kernel(...)`.

---

## 15. `quail/tools.py`

**Job.** The four operations as plain functions. MCP, CLI, and hosted
all call these.

**Why.** If `setup` lives in `mcp.py`, hosted has to parse MCP to import
a session.

### `setup(project) -> dict`

Orientation object in `kernel.md`:

- `documentation`: contents of `docs/api.md` (packaged path, pinned in
  `kernel.md`)
- `datasets`: `id`, `rows`, `fields` (source names)
- `sessions`: `name`, `dataset`, `cells`, `last_active`, `tag_fields`,
  `forked_from`, `open`, `orphan_tags`

Hash-check / rebuild if the index is missing or the CSV hash differs,
before reporting rows.

### `exec(project, code, session="default", dataset=None, fork_from=None) -> dict`

- Existing session: `dataset` omitted or must match.
- New session: require `dataset`, or the sole dataset if the project
  has exactly one.
- `fork_from` on first use of a new name: copy logs first
  (`project.py`).
- Hash-check CSV; spawn/reuse `Kernel`; return
  `{session, run, cell, output, error, tags_written, truncated, kernel_restarted}`.
- **Cell failures are results**, not thrown tool errors.
- Raise host `QuailError` only for: unknown dataset, session locked by
  **another process**, kernel failed to start, manifest invalid.

### `export(project, session, path=None) -> dict`

- Default `exports/<session>.csv`.
- `path` if given must stay inside the project.
- Columns: `id`, source columns, then **this session’s** tag fields;
  non-scalars JSON-encoded.
- `{path, rows, columns}`.

### `reset(project, session) -> dict`

Kill the kernel process. Tags untouched. `{session, ok: true}`.

### Must not contain

JSON-RPC / MCP types, argparse, expression internals, embed HTTP.

**Spec.** `kernel.md` MCP tools (payloads are the function contracts);
`storage.md` Export.

---

## 16. `quail/mcp.py`

**Job.** Stdio MCP server over `tools.py`.

**Why.** MCP versioning and hosted HTTP should not fork the product.

**Contains.** Register `quail_setup`, `quail_exec`, `quail_export`,
`quail_reset`. Arguments by name. Resolve `DIR` with `Project` from
`project.py`. Tool errors vs cell results already distinguished in
`tools.py`.

**Must not.** A second copy of exec semantics; authentication.

**Spec.** `kernel.md` MCP tools; README `quail mcp .`.

---

## 17. `quail/cli.py`

**Job.** Argv → `tools.py` / `project.py` / `index.py`.

**Why.** A harness with a shell and no MCP client still gets the
language.

Commands are exactly `kernel.md` CLI:

| Command | Calls |
| --- | --- |
| `quail init [DIR]` | write `quail.toml` skeleton, `.gitignore`, `sessions/` |
| `quail import CSV [--name N] [--id COL] [--embed PROVIDER/MODEL]` | add `[datasets.*]`, `index` import |
| `quail mcp [DIR]` | `mcp.py` |
| `quail exec SESSION FILE.py` | `tools.exec` in a **fresh** kernel; print the result |
| `quail sessions` | list from `project.py` + orphans from index |
| `quail fork SRC DST` | `project` fork |
| `quail fields DATASET [--session S]` | catalog; pin owner in `kernel.md`. SQLite `tags` can be stale after `git pull` until a kernel opens — the log map is the truth |
| `quail export SESSION [--out PATH]` | `tools.export` |
| `quail warm DATASET [--field F]` | `tools` → `kernel` (first `.semantic()` now) |

`quail exec` does **not** persist variables across invocations: each
call is its own kernel run. Tags persist because they are in the log.
That statement is `kernel.md` CLI, not `api.md` (MCP cells **do** keep
variables). Same product advice: put anything you want to keep in a
tag.

`init` / `import` write `quail.toml` on purpose (rule 9).

**Must not.** Evaluate the language in-process; HTTP server; call
`embed.py` directly.

**Spec.** `kernel.md` CLI; `storage.md` Manifest.

---

## 18. `tests/`

Plain `.py` files. No per-test folders. No `.txt` mirrors. No
`tests/__init__.py` — tests are not a package.

Name tests after the **behavior**, grouped by the module that owns it.
Do not create empty test files before the slice exists.

A reasonable layout once several steps exist:

| File | Proves |
| --- | --- |
| `test_names.py` | prelude public names ↔ `api.md` |
| `test_project.py` | manifest, paths, fork, flock, log parse, replay map |
| `test_index.py` | import, types-as-text, re-import, orphans |
| `test_produce.py` | pipeline produce table, predicate vs expression |
| `test_compile.py` | SQL / `q_` against `storage.md` tables |
| `test_verbs.py` | count / retrieve / values / tag / fields |
| `test_cell.py` | transaction, notebook display, `is`, truncation |
| `test_kernel.py` | spawn, restart flag, wall interrupt, embed forwarding |
| `test_tools.py` | setup payload, exec cell-error-vs-tool-error, export |
| `test_cli.py` | init writes skeleton; exec does not persist variables |

Every new public name in the prelude updates `api.md` and `test_names.py`
in the same change.

---

## 19. Hosted seams

Hosted is a wrapper. Core keeps these call-shaped and does not grow
policy around them.

| Seam | File | Hosted does |
| --- | --- | --- |
| `Project(path)` | `project.py` | Point at a per-user or per-workspace directory |
| `Kernel(project, session, spawn=…)` | `kernel.py` | Run `prelude.py` in a container/microVM with the project mounted |
| `embed` on the control channel | `kernel.py` → `embed.py` | Own providers, quotas, caches |
| `tools.setup / exec / export / reset` | `tools.py` | HTTP MCP + auth, after mapping the caller to a project |
| Log format | prelude writes; `project.py` parses | History views; import sessions done elsewhere |
| Index builder | `index.py` | CSV → `.quail/<dataset>.quail` |

The first five are the table at the end of `kernel.md`. The last is
`storage.md` What hosted attaches to. Core never contacts a remote
except the host’s embedding HTTP (and hosted may take that over).

---

## 20. When the spec is silent

Do not grow `api.md` with footnotes. Do not grow this guide into a spec.

1. Can the illegal state be a type error in `prelude.py`? Do that.
2. Does on-disk or process format need a bit the docs omitted? Write it
   into `storage.md` or `kernel.md` in the same PR as the code.
3. Only then a sentence in `api.md`, and only if the agent must see it.

**Real silences** (not already in signatures / tables):

- Digest algorithm over log files
- How prelude receives startup arguments (must be before DB open)
- Where packaged `docs/api.md` is found for `setup`
- Unknown `quail.toml` schema versions; unknown `session.toml` keys
- Source-`chunks` strategy on CSV re-import (duplicate split vs delete
  and rechunk)
- Whether `warm` is a cell or an extra control op
- `meta.embed_dims` vs the authorizer allow-list (no `meta`)
- `fields()` catalog order, if you want it stable for agents
- `quail fields` CLI: index catalog vs running `fields()` in a kernel.
  SQLite `tags` can be stale after `git pull` until a kernel opens;
  listing from the log map is the truth
- Tag `chunks` after CSV re-import (rowids change; on-open replay does
  not run if the log digest is unchanged)
- Failed cells append a log line (`kernel.md` step 6) and do not
  mention `applied`. If the digest covers all lines, a failure without
  an `applied` update forces replay on the next open — pin in
  `storage.md` / `kernel.md`

**Not silences** — enforce in prelude, already in `api.md`:

- `where=` is a predicate (examples); `by=` is an expression or list;
  `rank=` is a number expression
- First positional on `count` / `retrieve` is `where`
- `tag(target)` types are listed explicitly
