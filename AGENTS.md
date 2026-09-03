# Quail core

This branch is a from-scratch rebuild. `docs/` is the specification and
code follows it. Read `README.md` first, then the document that owns your
change.

## Documents

| Change | Owner |
| --- | --- |
| What the agent can write in a cell, what it gets back | `docs/api.md` |
| Files on disk, session logs, the index, expression → SQL | `docs/storage.md` |
| Kernel lifecycle, limits, sandbox, MCP tools, CLI, hosted seams | `docs/kernel.md` |

`api.md` says what happens; the other two say how. When they disagree, fix
both in the same change. `api.md` is returned to agents verbatim by
`quail_setup`, so every sentence in it costs context: keep it short and keep
it true.

## Design rules

- Prefer making a mistake unrepresentable over naming it. A new caveat or
  bespoke error message is the last resort.
- The sandbox is subtractive. Remove the network and the file system; never
  enumerate allowed Python.
- Tags are the only analysis state. Variables are working memory.
- The project directory is the truth; the SQLite index is derived and
  disposable. Core never runs git and never contacts a remote.
- Anything about who is calling or where a server is reachable from is
  hosted's concern. Core exposes seams (`docs/kernel.md`), not policy.
- Move one coherent step at a time.

## Conventions

- Python 3.12+. Plain modules under `quail/`, one file per concern, no
  mirror files. `quail/prelude.py` is self-contained and imports nothing
  else from the package.
- Tests are plain files under `tests/`. A test asserts that the public names
  in `prelude.py` and the names used in `docs/api.md` agree.
- Dependencies: `google-re2`, `mcp`; `numpy` optional. They land with the
  modules that need them.

## Code

Package layout is in `docs/kernel.md`. Implement one file at a time; do not
stub the rest.

| File | Now |
| --- | --- |
| `quail/project.py` | Load `quail.toml` and resolve project paths. Not yet: sessions, logs, replay, locks. |
