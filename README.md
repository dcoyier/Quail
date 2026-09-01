# Quail

Quail is an environment where agents analyze a text corpus: surveys, notes,
transcripts, and other collections that are worth deciding from and too
large to read exhaustively. The agent writes Python in a persistent kernel
against a small analysis language, and every conclusion it reaches is
backed by tags it can show and a log of the cells that produced them.

This branch is the ground-up rebuild of Quail core. It contains the design
and not yet the code. Three documents are the specification:

| Document | Reader | Contents |
| --- | --- | --- |
| [`docs/api.md`](docs/api.md) | the agent, at runtime | The analysis language. `quail_setup` returns this file verbatim. |
| [`docs/storage.md`](docs/storage.md) | implementers | Project layout, session logs, the derived SQLite index, how the language compiles onto it. |
| [`docs/kernel.md`](docs/kernel.md) | implementers | The kernel process, the cell contract, limits, MCP tools, CLI, and what hosted attaches to. |

## The model

- A **dataset** is an immutable grid of entries by fields, imported from a
  CSV.
- A **session** is a persistent Python kernel on one dataset plus a set of
  **tags**, session-scoped annotations that are the only analysis state.
- A **cell** is one `quail_exec` call. Variables persist across cells; tags
  commit per cell or not at all.
- The language has expressions (`Field("body").length()`), predicates
  (`… >= 500`), and four verbs: `count`, `retrieve`, `values`, `tag`.
  Expressions compile to SQL; search is an ordinary expression that yields a
  number.
- A **project** is a directory of text: manifest, CSVs, and one append-only
  log per kernel run. Git is how sessions move between agents and machines.
  SQLite is a derived index that is never committed.

## A project

```text
my-study/
  quail.toml
  data/notes.csv
  sessions/billing-coding/session.toml
  sessions/billing-coding/log/20260901T210000Z-a1b2c3.jsonl
  .quail/notes.quail        # derived, gitignored
```

```sh
quail init && quail import data/notes.csv
quail mcp .                 # stdio MCP: quail_setup, quail_exec, quail_export, quail_reset
```

An agent on another machine clones the repository, runs `quail mcp .`, and
continues the session where the log left off. Two agents in two sessions
push separate log files and merge without conflicts.

## Core and hosted

Core is this repository: the language, the kernel, the project format, a
stdio MCP server, and a CLI. Anything about who is calling or where a server
is reachable from belongs to Quail hosted, a separate repository that wraps
core. The seams hosted uses are listed at the end of `docs/kernel.md` and
`docs/storage.md`.

## Status

Design. The previous implementation is on `main`.

Apache-2.0 · Python 3.12+
