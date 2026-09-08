# Quail v0.94

Quail is an environment for agentic qualitative analysis. An agent (or human!)
works in a Python kernel to study a corpus of text. This works like a
notebook.

Anyone using Quail should also read [`USING_QUAIL.md`](USING_QUAIL.md) completely.
It is the manual for using Quail and details how to start and continue studies, the
analysis language, the local CLI, and how to share work.

For implementers, [`IMPLEMENTATION_GUIDE.md`](IMPLEMENTATION_GUIDE.md) is the
implementation contract.

Here's some vocab about Quail to provide an initial understanding:

- A **dataset** is an immutable grid of entries by fields (rows x columns),
  and it's imported from a CSV. Every entry has an `id` field, which is
  either provided or assigned by Quail.
- A **session** is a persistent workspace on one **dataset**, with its
  **tags** and recorded analysis history. Tags are values the user writes
  onto entries, scoped to that session. Its Python kernel holds working memory.
- A **cell** is one block of code that is submitted to the kernel.
  While the kernel is open, variables persist across cells. **Tags** are
  even more durable, lasting across kernel runs in a **session**. A cell's tag
  writes commit together or not at all, and are logged before you see its result.
- **Cells** use ordinary Python plus the **analysis language**: `Field`,
  reusable expressions and predicates, and four core verbs (`count`,
  `retrieve`, `values`, `tag`). Expressions are lightweight recipes you
  can combine and reuse in Python. The verbs execute inside the kernel,
  compiling expressions to SQLite queries. Source data stays read-only;
  `tag` writes session annotations.
- The last layer is a **study**, a directory of text files to keep things organized.
  It contains a concise config (`quail.toml`), **datasets**, one log per kernel
  run in each **session**, and optional shared embedding vectors. Tags and
  recorded history travel with the study through git.

## Installation

Quail runs on Linux and macOS with Python 3.12 or later. To install (with
git and [uv](https://docs.astral.sh/uv/getting-started/installation/)
already installed):

```sh
git clone --depth 1 https://github.com/dcoyier/Quail.git
cd Quail && uv sync --locked --no-dev --python 3.12 && . .venv/bin/activate
```

Quail lives in its own checkout; a study is a separate directory, usually
its own git repository. Keyword search works as is. Semantic search also
needs Ollama or an OpenAI-compatible endpoint, configured in `quail.toml`
for the dataset. You're now ready to read [`USING_QUAIL.md`](USING_QUAIL.md).

The goal of Quail is to provide a medium to *explore* a dataset, usually one with
plenty of text.

Apache-2.0 · Python 3.12+
