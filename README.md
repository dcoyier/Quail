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
| [`docs/api.md`](docs/api.md) | the agent, at runtime | The analysis language and the local stream. `quail setup` returns this file verbatim. |
| [`IMPLEMENTATION_GUIDE.md`](IMPLEMENTATION_GUIDE.md) | implementers | The implementation contract: observable behavior, module ownership, build order, and tests. |
| [`docs/storage.md`](docs/storage.md), [`docs/kernel.md`](docs/kernel.md) | implementers | Earlier design notes on the project format and the kernel. Where they differ from the guide, the guide is right; they are being folded into it. |

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

## How it is used

Installing Quail and starting a study are different steps. Quail lives in
its own checkout or environment; a study is a separate directory, normally
its own git repository, that Quail operates on. With git and
[uv](https://docs.astral.sh/uv/getting-started/installation/) installed:

```sh
git clone --depth 1 https://github.com/dcoyier/Quail.git
cd Quail && uv sync --locked --no-dev --python 3.12 && . .venv/bin/activate

quail init ../study && cd ../study
quail import notes.csv           # registers the dataset and builds its index
quail setup --json               # orientation: docs, fields, sessions, exact next commands
quail exec first-pass --stream   # one foreground kernel; JSON lines in, JSON lines out
```

The agent keeps that one process open and sends cells to it:

```text
{"op":"exec","code":"body = Field('body')\nparking = body.lexical('parking') > 0\ncount(parking)"}
{"op":"exec","code":"tag(parking, 'topic', 'parking')\ncount(by=Field('topic'))"}
```

Then `quail export first-pass` writes a CSV of the source fields plus the
session's tags.

Continuing someone else's work is a clone: `git clone` the study, then
`quail setup --json` and `quail exec first-pass --stream`. Indexes and tags
rebuild from the text on first open; nothing is re-imported. Two agents in two
sessions push separate log files and merge without conflicts. An agent
continuing another's session appends a new log file to the same session.

Semantic search is optional. Configure an embedding model and a fixed
revision at import (`--embed ollama/embeddinggemma --embed-revision v1`) or
in `quail.toml`, and `.semantic()` embeds a field the first time it is
searched. To do that work in parallel and share it, workers run
`quail warm notes --shard 1/4` through `4/4` and commit the resulting
`warm/` files; a fresh clone uses whatever parts have arrived.

If the CSV gains, loses, or edits rows and its `id` column is stable,
sessions continue: tags follow ids, removed entries are reported as
orphans, and new entries start untagged.

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
