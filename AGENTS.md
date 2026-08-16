# Quail v0.11

- For **running** Quail (clone, dataset, process/run), see `README.md`.
- Core semantics: `docs/core.md`. Prefer making a mistake unrepresentable over
  naming it — a new bespoke rejection message or doc caveat is a last resort.
- Model-facing analysis contract: `docs/api.md`.
- Change routing: `docs/development.md`.
- Connector author surface: `docs/connector-sdk.md`.
- Move slowly: one coherent step at a time.
- CLI must never write the operator TOML.

## Non-goals (still out)

- Operator console; validate/doctor/plan/apply ceremony
- Invitations / identity linking / live admin user APIs
- Search infrastructure extras (ANN, lexical workers/artifact roots)
- Hosting flourishes (ngrok as product; `public_base_url` remains)
- Connector events host API (file GET routes are in; pub/sub is not)

## Module layout (`.py` + `.txt`)

For a Python module `name`, use a folder pair — not a lone `.py` at the package root:

```
quail/.../name/
  __init__.py    # thin re-exports so `from quail....name import X` still works
  name.py        # implementation
  name.txt       # natural-language pseudocode mirror of name.py
```

Package `__init__.py` may also have a sibling `__init__.txt`. Review an existing pair (for example `quail/analysis/entry/`) before writing a new one.

**1:1 twin rule:** whenever a `.py` has a paired `.txt`, keep them in lockstep. Any behavior, symbol, param, validation, or stub change in the `.py` must be reflected in the `.txt` in the same change (and the reverse if you edit the `.txt` first). Do not land a diff that updates only one side of a twin pair.

Tests under `tests/` do **not** use this layout: plain `.py` test files only — no per-test folders and no `.txt` mirrors.

### `.txt` style

Mirror the `.py` in reading order. Write imperative prose, not Python and not architecture essays.

Typical voice:

- **Constants / secrets:** “There is…” / “There is a private secret called…”
- **Types:** “X is…” (one or two sentences of purpose)
- **Fields:** “X remembers:” then an indented name list (defaults noted inline)
- **Construction:** “To create X …:” then indented “Require…” / “Then remember…”
- **Methods / helpers:** a bare `name:` or `_name:` heading, then “Give back…”, “Raise…”, “If …, raise…”
- **Not-yet-wired:** “Meant to… Not wired yet, so raise…”

Keep sentences short. Prefer “give back”, “remember”, “require”, “raise” over implementation jargon. Name real symbols (`Field`, `QuailSyntaxError`, `_ENTRY_TOKEN`) when the code does. Do not dump “where this file sits” narratives into either the `.txt` or long module docstrings — short one-liners in the `.py` are enough; depth belongs in the `.txt` and in `docs/`.
