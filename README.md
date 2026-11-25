<p align="center">
  <img src="quail.png" alt="Quail">
</p>

# Quail

Quail lets AI agents search, count, and annotate a text dataset from a persistent Python session without changing the source data.

The Quail poster received Best Poster at US-RSE ’26: [Introducing Quail: An Environment for Agentic Qualitative Analysis](https://doi.org/10.5281/zenodo.23465689).

Agents using Quail should also read [`USING_QUAIL.md`](USING_QUAIL.md) completely.
It is the manual for using Quail and details the analysis language, the CLI,
how to share work, and how to start and continue studies.

## Try it

After [installing Quail](#installation), run this to create a fresh toy study. No embedding service is needed.

```sh
demo_dir="$(mktemp -d)/quail-demo"
quail init "$demo_dir"
cd "$demo_dir"
cat > notes.csv <<'CSV'
id,body
n1,The parking permit is too expensive.
n2,The staff were helpful.
CSV
quail import notes.csv
quail exec first-pass -c 'body = Field("body"); parking = body.lexical("parking") > 0; count(parking)'
# Expected output: 1
quail exec first-pass --close
```

## How Quail is organized

- A **dataset** is an immutable grid of entries by fields (rows x columns),
  and it's imported from a CSV. Every entry has an `id` field, which is
  either provided by the user or assigned by Quail.
- A **session** is a persistent workspace on one **dataset**, with its
  **tags** and recorded analysis history. Tags are values the user writes
  onto entries, scoped to that session. Its Python kernel holds your variables,
  functions, and classes.
- A **cell** is one block of code that is submitted to the kernel.
  While the kernel is open, variables persist across cells. **Tags** are
  even more durable, lasting across kernel runs in a **session**. A cell's tag
  writes commit together or not at all.
- **Cells** use ordinary Python plus the **analysis language**: `Field`,
  reusable expressions and predicates, and four core verbs (`count`,
  `retrieve`, `values`, `tag`). Expressions are lightweight recipes you
  can combine and reuse in Python. The verbs execute inside the kernel,
  compiling expressions to SQLite queries. Source data stays read-only;
  `tag` writes session annotations.
- The last layer is a **study**, a directory of text files to keep things organized.
  It contains a concise config (`quail.toml`), datasets, session logs recording
  code, output, and committed tag changes, and optional shared embedding vectors.
  Each kernel run creates its own log. These files are designed to travel with
  the study through Git.

## Installation

Quail runs on Linux and macOS with Python 3.12 or later. To install (with
git and [uv](https://docs.astral.sh/uv/getting-started/installation/)
already installed):

```sh
git clone --depth 1 https://github.com/dcoyier/Quail.git
cd Quail && uv sync --locked --no-dev --python 3.12 && . .venv/bin/activate
```

Semantic search through the **analysis language** requires also
setting up Ollama locally or an OpenAI-compatible endpoint, configured per study
and per dataset. You're now ready to read [`USING_QUAIL.md`](USING_QUAIL.md).
Apache-2.0 · Python 3.12+
