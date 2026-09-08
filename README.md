# Quail v0.94

Quail is an environment for agentic qualitative analysis. An agent (or human!)
works in a Python kernel to study a corpus of text. This works similar to a
notebook.

Anyone using Quail should also read [`USING_QUAIL.md`](USING_QUAIL.md) completely. 
It is the manual for using Quail and details how to start and continue studies, the 
analysis language, the local CLI, and how to share work.

Here's some vocab about Quail to provide an initial understanding: 
- A **dataset** is an immutable grid of entries by fields (rows x columns), 
  and it's imported from a CSV. Every entry has an `id` field, which is 
  either provided or assigned by Quail. 
- A **session** is a persistent Python kernel on one **dataset** plus its
  **tags**, values the user writes onto entries that are scoped to that session.
- A **cell** is one block of code that is submitted to the kernel. 
  While the kernel is open, variables persist across cells. **tags** are
  even more durable, lasting across kernels in a **session**.
- **cells** rely on the **analysis language**, a Pythonic DSL of four functions
  (`count`, `retrieve`, `values`, `tag`) and a few classes, to communicate 
  with the dataset. The classes are lightweight, reusable recipes used
  for the four funcions, and the functions themselves are like calling an API. 
  These functions are processed into SQLite queries outside of the kernel. This
  setup means that no kernel execution can ever modify the **dataset**.
- The last layer is a **study**, a directory of text files to keep things organized.
  It contains a concise config (`quail.toml`), **dataset(s)**, a log from each 
  kernel inside each **session**, and optional vector embeddings. This structure was 
  designed to easily transport work through git.

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
\
\
The goal of Quail is to provide a medium to *explore* a dataset, usually one with 
plenty of text. 

Apache-2.0 · Python 3.12+
