# Quail Core

The load-bearing semantics of the analysis language. Everything under
`quail/analysis/` implements this file; [`api.md`](api.md) is the agent-facing
derivation of it. When they disagree, this file wins and both change in the
same PR.

Keep this file short. It states what is true, not how to use it.

## The six statements

1. **A dataset is an immutable grid.** Entries × fields, JSON-like values.
   Absence is `None`. Imported source data never changes.
2. **A session adds an overlay.** Analysis fields and tags live on the
   session, scoped to one dataset version. Source fields cannot be created or
   overwritten. Bindings are session-scoped names restored on the next exec.
3. **The language builds inert descriptions.** An `Expression` pipes one
   field's value per entry through typed ops. A `Predicate` is a boolean per
   entry. A `GroupExpr` is a set of entries or fields, closed under `& | ~`.
   A `Ranking` is a non-negative linear combination of numeric expressions.
   A `Unit` picks what comes back. Construction never reads data.
4. **Evaluation happens only at four verbs.** `retrieve`, `count`, `tag`,
   `untag` (`entry.value` and `entry.fields` read through the same engine).
5. **An exec is a transaction.** Prints, tags, and bindings commit together or
   not at all. Later lines see earlier tags; failure rolls everything back.
6. **Search is not special.** `Lexical` and `Semantic` are ordinary ops that
   produce a score. Warm paths are optimizations, never semantics.

## Kinds and composition

Every op declares a signature over a small set of pipeline kinds:

| Kind | Meaning |
| --- | --- |
| `any` | An unread field value |
| `text` | One string |
| `number` | One finite float |
| `list_text` | `list[str]` |
| `text_or_list` | Text or `list[str]`, proven at runtime by a preceding op |
| `score` | A search relevance score; terminal |

**The rule:** a pipeline is legal iff each op accepts the kind the previous op
produced. `score` ends the pipeline. Rankable expressions are those whose
final kind is `number` or `score`.

| Op | Accepts | Produces |
| --- | --- | --- |
| `Value` | `any` (first position only) | unchanged |
| `AsText` | anything | `text` |
| `AsNumber` | `any`, `text`, `number`, `text_or_list` | `number` |
| `RegexSearch` | `any`, `text`, `text_or_list` | `text` |
| `RegexFindAll` | `any`, `text`, `text_or_list` | `list_text` |
| `RegexSub` | `any`, `text`, `list_text`, `text_or_list` | input kind (`any` → `text_or_list`) |
| `Slice` | `any`, `text`, `list_text`, `text_or_list` | input kind (`any` → `text_or_list`) |
| `Length` | `any`, `text`, `list_text`, `text_or_list` | `number` |
| `Lexical` | `any`, `text`, `list_text`, `text_or_list` | `score` (terminal) |
| `Semantic` | `any`, `text`, `list_text`, `text_or_list` | `score` (terminal) |

The canonical copy of this table is `OP_SPECS` in
`quail/analysis/operations/`. Pipeline validation and its error messages
derive from that table; nothing else may hand-check op order. Tests keep this
file and the op table in `api.md` in sync with `OP_SPECS`.

## Search queries

A `Lexical` / `Semantic` query is one rule: **a non-empty list of target
texts**, spelled as a string, a `list[str]`, an entry-scoped `GroupExpr`, or a
`list[Entry]` (entry shapes read each entry's expression root field). Scoring
maps each entry's pipeline value against those targets; aggregation is
`total` or `avg` on either side.

## The design rule

**Prefer making a mistake unrepresentable over naming it.**

When an agent trips on something, ask in order:

1. Is one of the six statements ambiguous? Fix the statement.
2. Is a row of the op table missing or wrong? Fix the row.
3. Only then consider wording — and improve the error *generator*, not one
   message.

A new bespoke rejection message or doc caveat is the last resort, not the
first move. Guardrails that narrate traps accumulate; shapes without the trap
do not.
