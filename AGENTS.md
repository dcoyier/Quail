# Quail core

This branch is a from-scratch rebuild. Read `README.md` first, then the
document that owns your change.

## Documents

| Change | Owner |
| --- | --- |
| What the agent can write in a cell, what it gets back, how it runs cells | `docs/api.md` |
| Everything else observable: project format, logs and replay, SQLite, language semantics, search, confinement, CLI, build order, tests | `IMPLEMENTATION_GUIDE.md` |
| Earlier design notes on storage and the kernel | `docs/storage.md`, `docs/kernel.md`. They are being folded into the guide; where they disagree with it, the guide is right (guide section 10). |

`api.md` says what happens; the guide says how, and what the code must do.
When they disagree, fix both in the same change. `api.md` is packaged as
`quail/data/api.md` and returned to agents verbatim by `quail setup`, so
every sentence in it costs context: keep it short and keep it true. Do not
add a second agent manual.

## Design rules

- Each open kernel sees one source snapshot. Stable `id`s carry a session
  across source edits; generated ids belong to one source version.
- Tags are the only analysis state; Python variables are working memory. A
  failed cell rolls back its tags and keeps its variables and output.
- The log decides what committed. A cell is acknowledged only after its
  record is fsynced to the run log; SQLite caches history and never competes
  with it.
- A cell writes only private working tables. Arbitrary Python and embedding
  waits never hold a writer transaction on the shared index.
- Caches do not define answers. Rebuilding the index, warming first, or
  another session's work must not change what a query means.
- The sandbox is subtractive: remove the network and the file system, keep
  ordinary Python. No allow-list of modules or syntax, and no ban on `is`.
- Ordinary Python is the extension mechanism. New capability comes from the
  four verbs composing with user code, not from new Quail abstractions,
  callbacks, or registries.
- Core never runs git, never calls a language model, and contacts no remote
  except the host's embedding provider calls. Who is calling and where a
  server is reachable from are hosted's concern; core exposes
  `open_session(..., spawn=, embed_fn=)`, not policy.
- Prefer making a mistake unrepresentable over naming it. A new caveat or
  bespoke error message is the last resort.
- Move one coherent step at a time.

## Layout

Plain modules under `quail/`, one file per responsibility, no mirror files:

| Module | Owns |
| --- | --- |
| `project.py` | Manifest, paths, metadata, locks, run-log writing and parsing, replay |
| `index.py` | CSV import, source indexes, materialized tags, vectors, warm packs, cache sync |
| `embed.py` | Provider HTTP only: the two dialects, timeouts, retries, response validation |
| `prelude.py` | Expressions, SQL compiler, verbs, private tag tables, scoring, cell execution, confinement |
| `kernel.py` | Child lifetime, control exchange, limits, durable cell completion, cached embeddings |
| `service.py` | Project operations, the one dataset-open path, `open_session`, export, warming |
| `cli.py` | Argument parsing, the foreground stream, presentation, exit status |

`prelude.py` is self-contained: it imports nothing else from the package,
parses no manifest or log, performs no provider HTTP, writes no durable
file, and contains no replay. The child starts as `python -m quail.prelude`;
importing `quail` must not load the host graph. Provider credentials and
HTTP stay in the host.

## Conventions

- Python 3.12+ on Linux and macOS. Dependencies: `google-re2` and `numpy`,
  both required; the standard library for everything else, including SQLite
  with FTS5 and HTTP. No MCP dependency in core.
- PEP 621 with Hatchling, a committed `uv.lock`, and
  `quail = "quail.cli:main"`. pytest, Ruff, and mypy for development.
- Tests are plain files under `tests/` using temporary projects and real
  SQLite; mock only the provider boundary. Organize them by the contracts in
  guide section 9. One test asserts the explicit public namespace of
  `prelude.py`; do not parse `api.md` for names.
- Benchmarks, generated corpora, and profiling output stay out of the
  repository.
- Build in the guide's slice order. Every slice ships through the real
  host/child boundary and the durable log.
