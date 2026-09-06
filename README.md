# Quail

Quail is an environment where an agent studies a corpus of text: survey
answers, support tickets, interview excerpts, field notes, any collection
that is worth deciding from and too large to read end to end. The agent
works in a persistent Python kernel with a small analysis language, decides
for itself what to look for and how to check it, and writes its judgments
back as tags. Every conclusion it reaches is backed by tags it can show and
a log of the cells that produced them.

```python
body    = Field("body")
parking = body.lexical("parking permit") > 0
count(where=parking, by=Field("dept"))
retrieve(rank=body.semantic("no place to park near work"), limit=5)
tag(parking, "topic", "parking")
```

| Document | Reader | Contents |
| --- | --- | --- |
| [`USING_QUAIL.md`](USING_QUAIL.md) | anyone using Quail, agents first | The manual: starting and continuing a study, the analysis language, the local stream, and sharing work. |
| [`IMPLEMENTATION_GUIDE.md`](IMPLEMENTATION_GUIDE.md) | implementers | The implementation contract: observable behavior, module ownership, build order, and tests. |

## How it works

- A **dataset** is an immutable grid of entries by fields, imported from a
  CSV. Every entry has a durable `id`, and the source is never modified.
- A **session** is a persistent Python kernel on one dataset plus its
  **tags**: values the agent writes onto entries, in fields it names. Tags
  are the only analysis state; variables are working memory.
- A **cell** is one submission to the kernel. Variables persist across
  cells. A cell's tags commit together or not at all, and are in the
  session log before the agent sees the result.
- The language is `Field`, expressions (`Field("body").length()`),
  predicates (`… >= 500`), and four verbs: `count`, `retrieve`, `values`,
  `tag`. Expressions compile to SQL. Keyword and semantic search are
  expressions that yield a number, so they filter, rank, and combine like
  any other.
- A **study** is a directory of text: a manifest, CSVs, one append-only log
  per kernel run, and optional shared embedding vectors. Git moves it
  between agents and machines, and sessions merge as separate files. SQLite
  is a derived index that is never committed.

## A study on disk

```text
my-study/
  quail.toml
  notes.csv
  sessions/first-pass/session.toml
  sessions/first-pass/log/20260901T210000Z-<uuid>.jsonl
  exports/first-pass.csv                                       # from quail export
  warm/notes/<source-version>/<plan>/part-0001-of-0004.jsonl   # optional shared vectors
  .quail/                                                      # derived index and locks, gitignored
```

## Installation

Quail runs on Linux and macOS with Python 3.12 or later. Installing it and
starting a study are separate steps: Quail lives in its own checkout, and a
study is a directory of its own, normally its own git repository, that
Quail operates on. With git and
[uv](https://docs.astral.sh/uv/getting-started/installation/) installed:

```sh
git clone --depth 1 https://github.com/dcoyier/Quail.git
cd Quail && uv sync --locked --no-dev --python 3.12 && . .venv/bin/activate
```

This installs Core from the default branch and puts `quail` on the path of
the activated shell. Keyword search works as is. Semantic search also needs
an embedding provider, a local Ollama or an OpenAI-compatible endpoint,
configured per dataset.

Continue with [USING_QUAIL.md](USING_QUAIL.md): it starts a first study,
runs the first cells, exports tags, and explains how agents share work.

## Core and hosted

Core is this repository: the language, the kernel, the study format, and a
CLI that a harness drives as one foreground process, JSON lines in and JSON
lines out. Core is the workbench, not the analyst: it runs no agent, calls
no language model, never runs git, and opens no network connection except
to a configured embedding provider. Authentication, an MCP server,
containers, and anything about who is calling or where a server is
reachable from belong to Quail hosted, a separate repository that wraps
Core's `open_session` and substitutes its own kernel spawn and embedding
calls.

Apache-2.0 · Python 3.12+
