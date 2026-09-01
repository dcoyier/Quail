# Kernel

How a cell runs. The agent's view is [`api.md`](api.md); the files it reads
and writes are in [`storage.md`](storage.md). This document is the process
between them.

## Shape

```text
agent ──MCP──▶ host process (quail mcp, or quail exec)
                  │  spawns one kernel per open session
                  ▼
               kernel subprocess: python -m quail.prelude
                  │  holds the SQLite connection and the run log
                  ▼
               .quail/<dataset>.quail   sessions/<name>/log/<run>.jsonl
```

The host owns the project directory, the manifest, the session locks, the
embedding providers, and the MCP or CLI surface. The kernel owns one
session: it runs cells, evaluates the language against the index, writes
the log, and has no network. They talk over the kernel's stdin and stdout
as JSON lines; the agent's own `print` output is captured separately and
never touches that channel.

## Cell 0: the prelude

The prelude is `quail/prelude.py`, one self-contained file. It imports only
the standard library, `re2`, and (optionally) `numpy`; nothing else from the
package. A container can run a kernel with just that file and the two
dependencies. The agent never reads it; `api.md` is the documentation of
its public names, and a test asserts the two agree: every name `api.md`
mentions in code exists in the prelude, and every public name the prelude
exposes appears in `api.md`.

The prelude runs once, with privileges, and then removes them. In order:

1. Parse arguments: project root, dataset, session, run id, limits.
2. Open `.quail/<dataset>.quail`. Register the `q_` functions. Install an
   authorizer that denies `INSERT`, `UPDATE`, `DELETE`, `DROP`, and `ALTER`
   on `entries` and `entries_fts` and allows them on `tags`, `tags_fts`,
   `chunks`, `vectors`, `applied`, and temp tables.
3. Digest the session's log files. If the digest differs from `applied`,
   replay (see `storage.md`). Cache the field catalog: source columns from
   `meta`, tag fields from `tags`.
4. Open `sessions/<name>/log/<run-id>.jsonl` for append and write the run
   header. Keep the handle; every later write goes through it.
5. Import the modules the agent gets for free: `re`, `math`, `statistics`,
   `json`, `itertools`, `collections`, `Counter`. Build the namespace: the
   verbs, `Field`, `Random`, `QuailError`, and a `quail` object holding the
   same names as attributes.
6. Set `RLIMIT_AS` from `kernel.memory_mb`.
7. Install the audit hook. It denies the events `open`, `os.remove`,
   `os.rename`, `os.mkdir`, `os.rmdir`, `shutil.*`, `sqlite3.connect`,
   `socket.*`, `subprocess.Popen`, `os.system`, `os.fork`, `os.exec`,
   `os.posix_spawn`, `ctypes.*`, `sys.addaudithook`, and `sys.setprofile`
   / `sys.settrace`. Hooks cannot be removed once added.
8. On Linux, `os.unshare(CLONE_NEWUSER | CLONE_NEWNET)` so the process has no
   network interface. If the kernel forbids it (some containers), continue
   with the hook alone and say so in the run header.
9. Report `ready` on the control channel and enter the cell loop.

Everything the kernel needs to touch later is already open by step 4: the
database connection and the log handle. SQLite's own file I/O is C-level
and raises no Python audit events, so the connection keeps working after
step 7 while agent code cannot open anything. Steps 6 through 8 are the
whole sandbox. There is no allow-list of Python constructs.

## The cell contract

Request from the host:

```json
{"op": "run", "code": "count(long)"}
```

Response:

```json
{"op": "result", "n": 12, "output": "412", "error": null, "tags_written": 0, "truncated": false}
```

`error` is `null` or `{"type": "QuailError", "message": "...", "hint": "..."}`.
Non-Quail exceptions carry their Python type name and message with `hint`
`null`.

Inside the kernel, one cell is:

1. Parse `code` with `ast`. If any `Is` or `IsNot` node is present, fail
   with a `QuailError` before executing anything: "`is` is not allowed;
   write `== None`". Any other `SyntaxError` is returned as such.
2. `BEGIN`.
3. Arm the CPU timer (`setitimer(ITIMER_VIRTUAL, cpu_seconds)`). Redirect
   `sys.stdout` and `sys.stderr` into a buffer.
4. Execute every statement but the last. If the last statement is an
   expression, evaluate it and, when the value is not `None`, append its
   `repr` to the buffer. This is the notebook display rule.
5. On success: build the cell record with the tag writes accumulated during
   the cell, append it to the log, `fsync`, `COMMIT`, update
   `applied.log_digest`, refresh the field catalog.
6. On any exception: `ROLLBACK`, discard accumulated tag writes, append a
   cell record with `error` set and `tags: []`, and format the traceback
   into the buffer with the prelude's own frames removed.
7. Disarm the timer. Restore streams. Truncate the buffer to
   `output_kib`, appending `\n[quail] output truncated at 64 KiB (of 1.2
   MiB)` when it was. Respond.

Assignments made before an exception are kept; the namespace is the
kernel's namespace and is never rolled back. This is stated to the agent in
`api.md`.

`tag()` records `{entry_id: value}` for every write it performs, per field,
in the order performed. Two writes to the same `(entry, field)` in one cell
collapse to the last; a literal `None` value or a computed `NULL` is
recorded as `null`.

## Services the kernel asks the host for

The kernel has no network, so anything that needs one is a request on the
control channel while the cell blocks:

```json
{"op": "embed", "model": "ollama/embeddinggemma:latest", "texts": ["…", "…"]}
{"op": "embedded", "vectors": [[0.01, …], …]}
```

The host batches, retries, and applies the provider from the manifest.
While a request is outstanding the host pauses the wall clock for that
cell; the kernel's CPU timer does not tick during I/O wait. A provider
failure returns `{"op": "embedded", "error": {...}}`, which the kernel
raises as a `QuailError` with the hint to check the provider.

Hosted attaches here to route embeddings through its own providers,
quotas, or caches without changing the kernel.

## Limits and recovery

| Limit | Mechanism | Default |
| --- | --- | --- |
| CPU per cell | `ITIMER_VIRTUAL` → `SIGVTALRM` → `QuailError` raised in the main thread | 30 s |
| Wall per cell | host timer → `SIGINT` (→ `KeyboardInterrupt`); after 5 s more, `SIGKILL` | 120 s |
| Memory | `RLIMIT_AS` at spawn → `MemoryError` in the cell | 1024 MiB |
| Output | truncation | 64 KiB |
| `retrieve` | `limit` clamped to `max_limit` with a note in the output; `values` is not capped, since scalars stay inside the kernel | 1000 |

The CPU and memory limits raise inside the cell, so they roll back like any
error and the kernel continues. The wall limit interrupts a cell that is
stuck in a long C call or a tight loop that outran the CPU timer; if the
interrupt is honored the cell fails normally. `SIGKILL` is the last resort.

A kernel that dies (killed, out of memory in a way it could not recover,
crashed) is restarted by the host on the next `quail_exec`. The prelude
runs again: the log digest matches, so nothing replays, and the new kernel
is ready in well under a second. Variables are gone. Tags are intact
because they were never in the kernel. The response carries
`"kernel_restarted": true` so the agent knows to rebuild its variables.

## Names

The verbs are ordinary names in the kernel namespace. `count = 0` shadows
`count` exactly as it would in a notebook. The prelude also exposes every
public name on a `quail` object (`quail.count`, `quail.Field`, …) so
recovery is one assignment away, and `quail_reset` exists for anything
worse.

The prelude's own objects (the connection, the log handle, the compiler)
live in a closure, not in the namespace. A determined agent can reach them
through introspection. In core that is accepted: the agent already has the
user's shell. Hosted is where a real boundary belongs, and the kernel is
designed to be wrapped: a container with the project mounted, the kernel
started from `prelude.py`, and embeddings served over the control channel.

## Locks and concurrency

The host takes `flock` on `sessions/<name>/.lock` before spawning a kernel
and holds it while the kernel lives. A second host process trying the same
session gets a `QuailError` naming the session and the holder. Cells for
one session are serialized by the host; a second `quail_exec` on the same
session waits its turn rather than failing.

Different sessions on the same dataset run in different kernels with their
own connections to the same index file. WAL mode makes that routine.
Two kernels warming the same field at once both embed and both
`INSERT OR IGNORE`; the work is duplicated, the result is not.

## MCP tools

Core exposes four tools over stdio (`quail mcp`). Arguments are passed by
name.

### `quail_setup()`

```json
{
  "documentation": "<contents of docs/api.md>",
  "datasets": [{"id": "notes", "rows": 4812, "fields": ["id", "title", "body"]}],
  "sessions": [
    {"name": "billing-coding", "dataset": "notes", "cells": 41,
     "last_active": "2026-09-01T22:14:09Z", "tag_fields": ["topic", "words"],
     "forked_from": "exploration", "open": false, "orphan_tags": 0}
  ]
}
```

Call it once. It is the whole orientation: the language, what data exists,
and what work already exists to continue.

### `quail_exec(code, session="default", dataset=None, fork_from=None)`

Runs one cell. If `session` exists, `dataset` must be omitted or match it.
If it does not exist, it is created for `dataset`, or for the project's
only dataset when there is exactly one; `fork_from` copies another
session's log first. Returns:

```json
{"session": "default", "run": "20260901T210000Z-a1b2c3", "cell": 12,
 "output": "412", "error": null, "tags_written": 0,
 "truncated": false, "kernel_restarted": false}
```

Cell failures are results, not tool errors, because iterating on a failed
cell is the normal loop. Tool errors are reserved for the things the agent
cannot fix inside a cell: unknown dataset, session locked elsewhere,
kernel failed to start, manifest invalid.

### `quail_export(session, path=None)`

Writes `exports/<session>.csv` (or `path`, inside the project) and returns
`{"path": "...", "rows": 4812, "columns": ["id", "title", "body", "topic", "words"]}`.

### `quail_reset(session)`

Kills the session's kernel and lets the next `quail_exec` start a fresh one.
Tags are untouched. Returns `{"session": "...", "ok": true}`.

## CLI

```text
quail init [DIR]                          write quail.toml, .gitignore, sessions/
quail import CSV [--name N] [--id COL] [--embed PROVIDER/MODEL]
quail mcp [DIR]                           serve the project over stdio MCP
quail exec SESSION FILE.py                run one cell in a fresh kernel and print the result
quail sessions                            list sessions: dataset, cells, last active, orphan tags
quail fork SRC DST                        copy a session's log into a new session
quail fields DATASET [--session S]        print the field catalog
quail export SESSION [--out PATH]
quail warm DATASET [--field F]            embed now instead of on first use
```

`quail exec` gives any harness with a shell the same language without an
MCP client. Variables do not persist between invocations because each is
its own kernel run; tags do, because they are in the log. That is the same
statement `api.md` makes about what to keep in a tag.

## Package layout

```text
quail/
  __init__.py
  prelude.py      the kernel: language, compiler, verbs, cell loop; self-contained
  project.py      manifest, paths, sessions, logs, replay, locks
  index.py        CSV → .quail/<dataset>.quail; schema; import and re-import
  embed.py        provider clients (Ollama, OpenAI-compatible); host side only
  kernel.py       spawn, control channel, timers, restart
  tools.py        setup / exec / export / reset as plain functions on a Project
  mcp.py          stdio MCP server over tools.py
  cli.py
docs/
  api.md  storage.md  kernel.md
tests/
```

Plain modules, one file per concern, no mirrors. `prelude.py` is the only
file that runs inside the sandbox and the only one that must not import
the rest of the package.

## What hosted attaches to

Hosted is a wrapper, and these are the handles it wraps:

| Seam | What hosted does with it |
| --- | --- |
| `Project(path)` | Points it at a per-user or per-workspace directory. |
| `Kernel(project, session, spawn=…)` | Substitutes a spawn strategy that starts `prelude.py` inside a container or microVM with the project mounted. |
| The `embed` request on the control channel | Serves it from its own providers, with quotas and caching. |
| `tools.setup / exec / export / reset` | Registers them on an HTTP MCP server behind authentication, after mapping the caller to a project. |
| The log format | Reads it for history views; writes it to import sessions done elsewhere. |

Anything about who is calling, where the server is reachable from, or how
projects are shared between people is hosted's, and none of it is
referenced by core.
