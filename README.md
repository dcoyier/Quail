# Quail

Quail is an environment where agents analyze a text corpus: surveys, notes,
transcripts, and other collections that are worth deciding from and too
large to read exhaustively. The agent writes Python in a persistent kernel
against a small analysis language, and every conclusion it reaches is
backed by tags it can show and a log of the cells that produced them.

This branch is the ground-up rebuild of Quail core. It contains the design
and not yet the code.

| Document | Reader | Contents |
| --- | --- | --- |
| [`USING_QUAIL.md`](USING_QUAIL.md) | agents using Quail | Starting and continuing studies, the analysis language, the local stream, and sharing work. |
| [`IMPLEMENTATION_GUIDE.md`](IMPLEMENTATION_GUIDE.md) | implementers | The implementation contract: observable behavior, module ownership, build order, and tests. |
| [`AGENTS.md`](AGENTS.md) | coding agents | Document ownership, design rules, and implementation conventions. |

## The model

- A **dataset** is an immutable grid of entries by fields, imported from a
  CSV. Every entry has a durable `id`.
- A **session** is a persistent Python kernel on one dataset plus a set of
  **tags**, session-scoped annotations that are the only analysis state.
- A **cell** is one submission to that kernel. Variables persist across
  cells; tags commit per cell or not at all, and are in the session log
  before the agent sees the result.
- The language has expressions (`Field("body").length()`), predicates
  (`… >= 500`), and four verbs: `count`, `retrieve`, `values`, `tag`.
  Expressions compile to SQL; search is an expression that yields a number.
- A **project** is a directory of text: manifest, CSVs, one append-only log
  per kernel run, and optional shared embedding vectors. Git moves it
  between agents and machines. SQLite is a derived index that is never
  committed.

## A project

```text
my-study/
  quail.toml
  notes.csv
  sessions/first-pass/session.toml
  sessions/first-pass/log/20260901T210000Z-<uuid>.jsonl
  warm/notes/<source-version>/<plan>/part-0001-of-0004.jsonl   # optional shared vectors
  .quail/                                                      # derived index and locks, gitignored
```

## Installation

Installing Quail and starting a study are different steps. Quail lives in
its own checkout or environment; a study is a separate directory, normally
its own git repository, that Quail operates on. With git and
[uv](https://docs.astral.sh/uv/getting-started/installation/) installed:

```sh
git clone --depth 1 https://github.com/dcoyier/Quail.git
cd Quail && uv sync --locked --no-dev --python 3.12 && . .venv/bin/activate
```

Continue with [USING_QUAIL.md](USING_QUAIL.md) to create or continue a study,
run the first analysis, export tags, and share work. It is the complete
usage manual after installation.

## Core and hosted

Core is this repository: the language, the kernel, the project format, and
a CLI whose stream a harness drives directly. Core never runs git, never
calls a language model, and opens no network connection except to a
configured embedding provider. Authentication, an MCP server, containers,
and anything about who is calling or where a server is reachable from
belong to Quail hosted, a separate repository that wraps core's
`open_session` and substitutes its own kernel spawn and embedding calls.

## Status

Design. `IMPLEMENTATION_GUIDE.md` is the contract for the build; the
commands above describe the target and do not run yet. The previous
implementation is on `main`.

Apache-2.0 · Python 3.12+
