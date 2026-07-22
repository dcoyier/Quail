# Quail Analysis API

This document is the **model-facing analysis contract** for Quail v0.11 — the
true product core. Implementation may land in stages; when code and this file
disagree, update one deliberately so they match. There is no separate `qir`
surface; public specs use ordinary reconstructible forms.

## Vocabulary at a High Level

| Term | High-level meaning |
|---|---|
| Dataset | The collection of source entries selected for one `quail_exec` call. |
| Session | The durable analysis context that retains successful bindings and analysis fields and tags created within that session. |
| Scope | The session, dataset version, and population type within which a value or specification is valid. |
| Entry | One item in the selected dataset. Think of this as a row. |
| Field | A reference to one value for entries, such as source text or a session-created analysis label. Think of this as a column in the dataset. |
| Source field | A field imported with the dataset. Its definition and values are immutable. |
| Analysis field | A session-scoped field created with `create_field()`. Its entry values are managed with `tag()` and `untag()`. |
| Registered field | A known source or analysis field. A registered field may still be absent from a particular entry. |
| Value | The content stored in a field for one entry, or a value computed from that content. |
| JSON-like value | `None`, a string, boolean, bounded integer, finite float, list of JSON-like values, or dictionary with string keys and JSON-like values. Individual API calls may narrow this set. |
| Symbolic | Describes a recipe or selection that Quail has not materialized yet, such as an expression, predicate, group, or ranking. |
| Expression | A symbolic recipe based on one field that reads or transforms that field’s value for each entry. Separate expressions can be combined in predicates. |
| Operation | One transformation inside an expression, such as `AsText()`, `RegexSearch()`, or `Length()`. |
| Predicate | A symbolic true-or-false condition evaluated for each entry, usually created by comparing an expression with another expression or value. |
| Group | A symbolic population of entries or fields. A group describes what is included; it is not a materialized Python list. |
| Unit | A specification of what `retrieve()` or `count()` should return or count, such as entries, fields, projected field values, or distinct values. |
| Ranking | A symbolic ordering rule built from one or more numeric score expressions. |
| Materialized result | Concrete data returned at an evaluation boundary, such as a list from `retrieve()`, an integer from `count()`, or an entry value. |
| Binding | A top-level variable name whose supported value persists after a successful execution and can be reused only in the same session and dataset scope. |
| Mutation | A session-scoped change created with `create_field()`, `tag()`, or `untag()`; imported source data remains unchanged. |

## The `quail_exec` Tool

### Signature

```text
quail_exec(session_id, dataset_id, code, time_window="standard")
```

MCP clients should pass arguments by name. `session_id`, `dataset_id`, and `code` are required strings. `time_window` is optional, accepts `"standard"` or `"extended"`, and defaults to `"standard"` when omitted.

### Parameters

- `session_id`: the session to execute in. Sessions surface bindings and mutations specific to that session.
- `dataset_id`: the singular dataset selected for this execution.
- `code`: bounded Quail Python to validate and execute.
- `time_window`: selects the deployment-configured execution-time window. `"standard"` uses the normal bounded wall-clock and CPU-time limits. `"extended"` uses a longer bounded window for analysis that cannot complete within the standard window. Both windows remain finite and subject to cancellation and every non-time resource limit.

### Invocation Workflow

Before the first analysis call, read this document. Discover an authorized dataset with `quail_list_datasets` when that tool is exposed, start one durable session with `quail_start_session`, read the selected dataset's guidance with `quail_get_dataset_info`, and reuse the returned `session_id` in later `quail_exec` calls.

### Success and Failure Results

Success returns `{"printed_output": text}`, where `text` is exactly the buffered output from `print()`. Failure returns a tool error containing `execution_id` and `diagnostic`; `execution_id` is `None` when the request was rejected before an execution began. Clients may wrap the payload, but the structured fields remain the public repair contract.

## Start Here

### Inspect Fields and Value Shapes

Always inspect the available fields. Increase limit if needed.

```python
for field in retrieve(unit=fields, group=G1, limit=50):
    print(field.name, field.kind)

samples = retrieve(limit=1)
if len(samples) > 0:
    sample = samples[0]
    for field in sample.fields():
        print(field.name, repr(sample.value(field)))
```

### Retrieve Entries

`retrieve()` always returns a list. Entry retrieval is in dataset processing order when no rank is supplied.

```python
for entry in retrieve(group=G0, limit=10):
    print(entry.id)
```

### Filter Entries

Create an expression, compare it to create a predicate, and apply that predicate to an entry group. This example assumes inspection showed a text field named `content`.

```python
content = Field("content")
mentions = Expression(content, RegexSearch("hydrangea", flags=re.I)) != None
matching = G0.where(mentions)

print("matches", count(group=matching))
for entry in retrieve(group=matching, limit=10):
    print(entry.id, entry.value(content)[:500])
```

### Rank Entries

Create a numeric score expression, optionally narrow the candidate group, wrap the score in a `Ranking`, and use the same group, rank, order, and limit when retrieving aligned entries and scores.

```python
score = Expression(Field("content"), Lexical("hydrangea care"))
matching = G0.where(score > 0)
rank = Ranking(expression=score)
ranked_entries = retrieve(group=matching, rank=rank, limit=10)
ranked_scores = retrieve(unit=score, group=matching, rank=rank, limit=10)

for index in range(len(ranked_entries)):
    print(ranked_entries[index].id, ranked_scores[index])
```

### Mutate Session Dataset

Source fields are immutable. Create an analysis field, tag or untag a session-scoped selection, then verify the result. Mutations become visible to later statements in the same successful execution and persist only for the same session and dataset version.

```python
selected = G0.where(Expression(Field("content"), RegexSearch("climate", flags=re.I)) != None)
topic = create_field("topic")
tag(selected, topic, "climate")
print(count(group=G0.where(Expression(topic, Value()) == "climate")))
```

## API Contract

### Product Boundary

Quail's public product is a composable qualitative-analysis interface expressed through bounded Python. A program builds inspectable symbolic specifications, materializes only bounded results, controls caller-visible analysis through `print()`, persists supported session state, and never changes imported source data.

### Execution Scope

Each `quail_exec` call selects exactly one authorized dataset and its active immutable version. Quail code receives no database handles, filesystem paths, credentials, environment variables, network access, import capability, or authority beyond the selected session and dataset scope.

### Output Contract

Only text explicitly written with `print()` by a successful execution is returned as analysis output. A successful execution with no `print()` returns an empty string. Failures return a structured diagnostic instead of partial output.

### Session and Dataset Isolation

Sessions belong to one authenticated principal and workspace. Analysis fields, tagged values, and persisted bindings are isolated by session and dataset version; another session or dataset version cannot observe them. Imported source fields and values remain immutable.

### Atomicity

One execution is atomic across analysis mutations, persisted bindings, audit state, and printed output. Later statements in the same program see earlier successful mutations, but nothing becomes durable and no output is released unless the complete execution succeeds.

### Limits and Cancellation

Deployments bound code size, wall-clock and CPU time, memory, output, API calls, result items, mutations, materialized data, bindings, and protocol traffic. `time_window="standard"` applies the normal execution-time bounds, while `time_window="extended"` applies longer deployment-configured bounds. Both windows remain finite, and caller cancellation and server shutdown remain effective for every execution.

## Public Namespace

### Injected Namespace

Quail injects the complete public namespace below into each execution. All imports are unavailable. Injected names are reserved.

| Category | Injected names |
|---|---|
| Functions | `retrieve`, `count`, `create_field`, `tag`, `untag`, `print` |
| Base groups and units | `G0`, `G1`, `entries`, `fields` |
| Types | `Field`, `Unit`, `Expression`, `Predicate`, `GroupExpr`, `Ranking`, `Entry` |
| Expression constructors | `Value`, `AsText`, `AsNumber`, `RegexSearch`, `RegexFindAll`, `RegexSub`, `Slice`, `Length`, `Lexical`, `Semantic` |
| Regex helper | `re` |
| Error classes | `QuailError`, `QuailSyntaxError`, `QuailScopeError`, `QuailFieldError`, `QuailRuntimeError` |
| Safe built-ins | `abs`, `all`, `any`, `bool`, `dict`, `enumerate`, `float`, `int`, `len`, `list`, `max`, `min`, `range`, `repr`, `round`, `set`, `str`, `sum`, `tuple` |

### `G0` and `G1`

`G0` is the built-in entry group containing every entry in dataset import order. `G1` is the built-in field group containing source fields followed by analysis fields. Both are symbolic `GroupExpr` values, not functions or iterable result lists.

### `entries` and `fields`

`entries` is `Unit("entries")` and is the default result unit for `retrieve()` and `count()`. `fields` is `Unit("fields")` and must be paired with a field-scoped group such as `G1`. They are result-unit values, not groups, strings, or functions.

### `re`

`re` is a restricted regex helper, not the Python `re` module. It exposes `re.escape(pattern)`, `re.NOFLAG`, and the portable flag aliases `re.A`, `re.ASCII`, `re.I`, `re.IGNORECASE`, `re.M`, `re.MULTILINE`, `re.S`, `re.DOTALL`, `re.U`, and `re.UNICODE`.

### Error Classes

`QuailError` is the base class for `QuailSyntaxError`, `QuailScopeError`, `QuailFieldError`, and `QuailRuntimeError`. Exception handling syntax is unavailable inside `quail_exec`; the injected classes identify the error categories used by public diagnostics.

## Execution and Composition Model

### Symbolic and Materialized Values

`Field`, `Unit`, operation objects, `Expression`, `Predicate`, `GroupExpr`, and `Ranking` are immutable specifications that describe later work. An `Entry` is an immutable runtime-issued identity handle, not a copied row. `retrieve()` returns a materialized list, `count()` returns a materialized integer, and `entry.value()` and `entry.fields()` return materialized entry data. Expressions, predicates, groups, rankings, and entries cannot be used as Python truth values; expressions and groups also cannot be iterated directly.

### Operators and Result Types

| Form | Result |
|---|---|
| `expression < literal`, `<=`, `>`, `>=`, `==`, or `!=` | `Predicate` |
| `expression_a < expression_b`, `<=`, `>`, `>=`, `==`, or `!=` | `Predicate` |
| `predicate_a & predicate_b`, `predicate_a \| predicate_b`, `~predicate` | `Predicate` |
| `group_a & group_b`, `group_a \| group_b`, `~group` | `GroupExpr` |
| `expression_or_ranking_a + expression_or_ranking_b` | `Ranking` |
| `expression * weight` or `ranking * weight` | `Ranking` |

Use parentheses when mixing symbolic operators. Python `and`, `or`, `not`, chained comparisons, and `is` do not compose symbolic values; use `&`, `|`, `~`, separate comparisons, and `== None` or `!= None` instead. Ranking multiplication accepts the weight only on the right.

### Evaluation Boundaries

Constructing or composing a symbolic value does not read the dataset. Quail evaluates symbolic work when it is passed to `retrieve()`, `count()`, `create_field()`, `entry.value()`, `entry.fields()`, `tag()`, or `untag()`; field resolution, scope checks, data conversion, predicate evaluation, and search scoring can therefore fail at those boundaries. Ordinary Python operators and built-ins operate only on already materialized local values unless an operator is documented here as symbolic.

### Retrieval Processing Order

Retrieval processes group selection, field-presence filtering or distinct-value deduplication when required by the unit, ranking, bounded window selection, and projection, in that order. A non-empty ranking scores every candidate remaining before the window, regardless of `limit`, so narrow the group first when possible. `order="top"` takes the forward ranked or processing-order window, `order="bottom"` reverses that order including ties, and `order="middle"` takes a centered window while preserving forward order.

### Materialization Paths

| Need | Materialization path |
|---|---|
| Bounded entries, fields, values, or expression results | `retrieve(unit=..., group=..., limit=...)` |
| Population, present-value, or distinct-value size | `count(unit=..., group=...)` |
| One registered field on one entry | `entry.value(field, default=None)` |
| Present fields on one entry | `entry.fields()` |
| Caller-visible text | `print(...)` followed by successful completion |

### Accepted Argument Types

`retrieve()` and `count()` accept only the documented `Unit` and `GroupExpr` scopes; only `retrieve()` accepts an `Expression` unit, a `Ranking`, a window order, and a limit. Predicates become populations through an entry group such as `G0.where(predicate)`. Mutations accept an entry `GroupExpr` or `list[Entry]` and an analysis `Field`. Search queries accept text, `list[str]`, an entry `GroupExpr`, or `list[Entry]`. Passing a symbolic value in the wrong role is an error rather than an implicit conversion.

### Mutation Visibility

Later statements in one execution see analysis fields and values created or changed by earlier statements. Existing symbolic expressions, predicates, groups, and rankings are recipes and are re-evaluated against the current staged analysis state when next materialized. A failed execution discards every staged mutation.

### Binding Persistence

After complete success, Quail snapshots supported top-level names assigned by the program and removes persisted names explicitly deleted by the program. A later execution in the same session and dataset-version scope receives those bindings alongside the injected namespace. Local names created only inside a loop still belong to the top-level execution namespace and must also be persistable or deleted before completion.

## Data Types

### `Field`

```python
Field(name, kind=None)
```

`name` is a non-empty case-sensitive string. `kind` is `"source"`, `"analysis"`, or `None`; `None` resolves the field by name, while an explicit kind must match. Public attributes are `.name` and `.kind`. `Field` has no public methods, and direct field comparisons are invalid inside `quail_exec`; compare `Expression(field, Value())` instead.

### `Unit`

```python
Unit(scope, field=None)
```

`scope` is `"entries"`, `"fields"`, or `"values"`. `Unit("entries")` retrieves entries; `Unit("entries", field)` retrieves present field values aligned to entries; `Unit("fields")` retrieves fields; and `Unit("values", field)` retrieves distinct present field values. `Unit("fields", field)` and `Unit("values")` are invalid. Public attributes are `.scope` and `.field`.

### `Entry`

`Entry` has no public constructor. Quail issues entry handles through `retrieve()`. Public attributes are `.id`, `.dataset_id`, `.dataset_version_id`, and `.dataset`; handles are immutable and scoped to their issuing dataset version.

```python
entry.value(field, default=None)
entry.fields()
```

`entry.value()` accepts a `Field` or field-name string, returns `default` for an absent registered field, and raises `QuailFieldError` for an unknown field. `entry.fields()` returns the present fields as `list[Field]`. Entries do not support subscripting or dynamic field attributes.

## Retrieval

### `retrieve()`

```python
retrieve(unit=entries, group=G0, limit=1, order="top", rank=Ranking())
```

`unit` accepts a `Unit` or `Expression`; `group` accepts a scope-compatible `GroupExpr`; `limit` is a positive integer; `order` is `"top"`, `"middle"`, or `"bottom"`; and `rank` is a `Ranking`. The function always returns a list.

### `count()`

```python
count(unit=entries, group=G0)
```

`unit` accepts an entry, field, projected-entry, or distinct-value `Unit`, but not an `Expression`. `group` must be a scope-compatible `GroupExpr`. The function returns a non-negative integer.

### Materialized Result Lists

Every `retrieve()` call returns a bounded list, including an empty list when nothing is selected. Result lists support ordinary bounded list operations such as `len()`, iteration, indexing, and slicing, and can persist when every contained value is persistable and binding limits are satisfied.

| `unit` | Compatible `group` | Each returned item | Non-empty `rank` |
|---|---|---|---|
| `entries` or `Unit("entries")` | Entry group | Runtime-issued `Entry` | Allowed |
| `fields` or `Unit("fields")` | Field group | `Field` | Unavailable |
| `Unit("entries", field)` | Entry group | Present value of `field` for one entry | Allowed |
| `Unit("values", field)` | Entry group | Distinct present value of `field` | Unavailable |
| `Expression` | Entry group | Computed value for one entry | Allowed |

Projected field-value retrieval excludes entries where the registered field is absent. Distinct-value retrieval also deduplicates in first-seen order. Empty strings and empty lists are present values. Expression retrieval retains one computed result per selected entry, including `None`, empty strings, and empty lists. Persisted entry handles remain valid only in their issuing dataset-version scope.

## Expressions and Operations

### Expression Model

```python
Expression(input, operation, ...)
```

`input` is a `Field` or `Expression`, followed by one or more operation objects. Nesting an existing expression appends and flattens its operation pipeline. Public attributes are `.input` and `.operations`; expressions are symbolic, cannot be iterated or used as Python truth values, and are materialized with `retrieve(unit=expression, ...)`.

### `Value()`

```python
Value()
```

Returns the current field value unchanged. `Value()` is the identity operation and is valid only as the first operation in an expression pipeline.

### `AsText()`

```python
AsText()
```

Converts the current value to canonical text. `None` becomes `""`; strings remain unchanged; booleans become `"True"` or `"False"`; finite numbers become JSON numeric text; and lists or dictionaries become compact canonical JSON text. The result is always text.

### `AsNumber()`

```python
AsNumber()
```

Converts a non-boolean finite number or a JSON-style numeric string with optional surrounding ASCII whitespace to a finite binary64 float. It rejects `None`, booleans, empty or nonnumeric strings, lists, dictionaries, overflow, and non-finite results with `QuailRuntimeError`.

### `RegexSearch()`

```python
RegexSearch(pattern, flags=0)
```

Searches text for the first match and returns the complete matched substring, or `None` when no match exists. Capture groups do not change the returned value to a captured subgroup. Input must be text or `None`; `None` is treated as empty text.

### `RegexFindAll()`

```python
RegexFindAll(pattern, flags=0)
```

Returns `list[str]` containing every complete match in encounter order. Capture groups do not change each item to a captured subgroup. Input must be text or `None`; `None` is treated as empty text and therefore normally produces an empty list unless the pattern matches empty text.

### `RegexSub()`

```python
RegexSub(pattern, replacement, flags=0)
```

Replaces every match with literal replacement text. Backreference and capture expansion in `replacement` are not supported. Input may be text, `list[str]`, or `None`; text input returns text, list input is transformed item by item and returns `list[str]`, and `None` is treated as empty text.

### `Slice()`

```python
Slice(start, end=None)
```

Applies the Python-style `[start:end]` slice to text. Input may be text, `list[str]`, or `None`; list input applies the same slice to every string rather than slicing the list itself, and `None` is treated as empty text.

### `Length()`

```python
Length()
```

Returns the character count of text, the item count of a list, or `0` for `None`. Other value shapes raise `QuailRuntimeError`.

Expression pipelines are type-checked in order. Use `AsText()` before a regex operation when a field may contain non-text values; `RegexSub()` and `Slice()` can transform the `list[str]` produced by `RegexFindAll()`; and `Length()` converts text or a list into a rankable number.

Operation objects expose only their documented constructor data: regex operations expose `.pattern` and `.flags`, `RegexSub()` also exposes `.replacement`, and `Slice()` exposes `.start` and `.end`. `Value()`, `AsText()`, `AsNumber()`, and `Length()` have no public data attributes.

## Regular Expressions

### Engine and Unicode Semantics

Quail regex operations use a bounded linear-time RE2-compatible engine rather than Python's backtracking `re` engine. Patterns operate on Unicode text. By default, `\d`, `\s`, `\w`, `\b`, and `\B` use Unicode semantics; `re.A` changes shorthand classes to ASCII, POSIX classes remain ASCII, and explicit `\p{...}` and `\P{...}` properties remain Unicode.

### `re.escape()`

```python
re.escape(pattern)
```

`pattern` must be text. The function returns the text escaped for literal use in a Quail regex operation.

### Supported Flags and Aliases

| Value | Meaning |
|---|---|
| `0` or `re.NOFLAG` | Default Unicode matching. |
| `re.A` or `re.ASCII` | Use ASCII semantics for shorthand character classes. |
| `re.I` or `re.IGNORECASE` | Ignore case. |
| `re.M` or `re.MULTILINE` | Make `^` and `$` operate at line boundaries. |
| `re.S` or `re.DOTALL` | Make `.` match a newline. |
| `re.U` or `re.UNICODE` | Explicitly request the default Unicode mode. |

Combine compatible flags with `|`. Flags are integer values, but only the listed bits are accepted, and `re.A` and `re.U` cannot be combined.

### Unsupported Constructs

Look-around assertions, backreferences, conditionals, atomic or other advanced group forms, verbose-mode `re.X`, and raw-byte `\C` are unsupported. Rewrite a pattern with consuming groups, ordinary capturing or non-capturing groups, explicit alternation, or separate predicates. Capture groups are allowed, but `RegexSearch()` and `RegexFindAll()` still return complete matches, and `RegexSub()` always treats replacement text literally.

### Compilation and Runtime Errors

Regex constructors validate the pattern, flags, Unicode text, bounded pattern size, and bounded compiled complexity immediately. Invalid or unsupported syntax raises `QuailSyntaxError` at construction. A valid regex can still raise `QuailRuntimeError` when materialization supplies an unsupported input value shape; use `AsText()` first when inspection shows mixed non-text values.

## Predicates

### Construction by Comparison

Compare an `Expression` with a JSON-like literal or another `Expression`. The comparison returns a symbolic predicate immediately; it does not compare Python objects or read entry values until Quail evaluates a containing entry group.

```python
content = Expression(Field("content"), Value())
has_text = content != None
same_as_title = content == Expression(Field("title"), Value())
long_enough = Expression(Field("content"), Length()) >= 500
```

### Direct `Predicate` Construction

```python
Predicate(left, operator, right=None)
```

Prefer constructing predicates with expression comparison and predicate composition operators. Direct construction accepts an `Expression` left operand with one of `"<"`, `"<="`, `">"`, `">="`, `"=="`, or `"!="`; two `Predicate` operands with `"and"` or `"or"`; or one `Predicate` left operand with `"not"` and the right operand omitted. Public attributes are `.left`, `.operator`, and `.right`.

### Comparison and Logical Operators

Comparison operators are `<`, `<=`, `>`, `>=`, `==`, and `!=`. Compose predicates with `&` for conjunction, `|` for disjunction, and `~` for inversion. Python `and`, `or`, and `not`, predicate truth testing in `if` or `while`, chained comparisons, and `is None` are invalid because they request an immediate Python truth value.

### Type, Numeric, and Null Semantics

Equality and inequality accept another `Expression` or a finite JSON-like literal: `None`, a boolean, number, string, list, or string-keyed dictionary. Equality is structural; booleans remain distinct from numbers, while numerically equal integers and floats such as `1` and `1.0` compare equal. Ordering accepts another `Expression` or a finite numeric literal and requires both evaluated operands to be finite non-boolean numbers; an incompatible or absent value is a runtime error, not `False`. Test null or absent expression results with `== None` or `!= None`.

### Short-Circuit Evaluation

Predicate composition evaluates left to right. `a & b` skips `b` when `a` is false, and `a | b` skips `b` when `a` is true. Expressions are pure, so short-circuiting affects evaluation work and whether a data-dependent error is encountered, never mutation order.

### Accepted Call Sites, Persistence, and Errors

Predicates are accepted by `G0.where(predicate)`, another entry group's `.where(predicate)`, or `GroupExpr("entries", predicate=predicate)`. They are not result units, field filters, mutation targets by themselves, or Python booleans. Predicates can persist as bindings; they re-evaluate when a later materialization uses them. Invalid operand or composition shapes fail during construction, while field resolution, conversion, search, and comparison failures caused by entry data occur during group materialization.

## Groups

### Base Populations and Filtering

`G0` is every entry in import order and `G1` is every source field followed by every analysis field. `entry_group.where(predicate)` returns the intersection of that group with the entries satisfying the predicate. Predicates and `.where()` are entry-scoped; filter field populations by constructing or composing field member groups instead.

### Explicit Members

`GroupExpr("entries", members=entry_list)` creates a population from runtime-issued entries, and `GroupExpr("fields", members=field_list)` creates one from fields. Members must match the declared scope. Materialization removes duplicate members while preserving the first occurrence; entry handles must belong to the active dataset version, and fields must resolve in the active field registry.

### `GroupExpr` Construction and `where()`

```python
GroupExpr(scope, predicate=None, members=None)
group.where(predicate)
```

`scope` is `"entries"` or `"fields"`. Public construction requires exactly one of `predicate` or `members`; predicates are entry-scoped, entry members are `list[Entry]`, and field members are `list[Field]`. `where()` accepts a `Predicate` and is valid only on entry-scoped groups. Public attributes are `.scope`, `.predicate`, `.members`, `.left`, `.operator`, and `.right`.

### Scope, Composition, Ordering, and Materialization

Compose equal-scope groups with `&` for intersection, `|` for union, and `~` for complement. Intersection preserves the left group's order, union appends unseen members from the right group, and complement follows `G0` or `G1` order. Groups cannot be iterated, indexed, or used as Python truth values; materialize entry groups with `retrieve(group=group, ...)` or `count(group=group)`, and field groups with `retrieve(unit=fields, group=group, ...)` or `count(unit=fields, group=group)`.

### Persistence and Errors

Groups can persist as bindings and remain symbolic. Predicate groups re-evaluate their predicates, while explicit member groups retain their member identities. Combining entry and field scopes, applying a predicate to fields, using a handle from another dataset version, or pairing a group with an incompatible unit raises a scope or field diagnostic rather than coercing the population.

## Ranking and Search

### Ranking Model

A non-empty ranking assigns one finite numeric score to each candidate entry and orders larger scores first. Rankable expressions must end in `AsNumber()`, `Length()`, `Lexical()`, or `Semantic()`. `Ranking()` preserves processing order and is the identity for addition; `+` sums signals, and right-side multiplication applies a finite non-negative weight. Signal scales are not normalized, so weights are meaningful only relative to the score ranges actually produced. Equal scores preserve candidate order before the requested window order is applied.

Use the same group, ranking, order, and limit to retrieve aligned entries and score-expression results. A non-empty ranking is available for entry, projected field-value, and expression retrieval, but not field or distinct-value retrieval. Reverse multiplication, subtraction, division, and negative weights are unsupported.

### `Ranking`

```python
Ranking(expression=None)
```

Omitting `expression` creates the empty ordering-preserving ranking; otherwise use a rankable expression as described above. Public attributes are `.expression`, `.left`, `.operator`, and `.right`; composed rankings expose their symbolic composition through those attributes.

### Search Targets and Aggregation

`Lexical()` and `Semantic()` accept one non-empty string, a `list[str]` containing at least one non-empty string, an entry-scoped `GroupExpr`, or a non-empty `list[Entry]`. Literal strings are target text. Entry and group targets read the same root field as the scored expression; target entries remain eligible candidates unless the caller excludes their member group.

The value entering the search operation for one candidate may be text, `list[str]`, or `None`. Text supplies one input segment, a list supplies multiple segments, and `None` or empty text supplies no segment. `input_aggregation` combines a candidate's segment scores and `target_aggregation` combines scores across targets; each accepts `"total"`, `"avg"`, or `None`, with `None` meaning `"total"`. An input with no usable segment scores `0.0`; a query whose resolved targets contain no usable text is an error. Target multiplicity is preserved, so repeated targets contribute repeatedly.

Both operations must end an expression pipeline and return finite scores where larger means more relevant. Neither operation promises a fixed public score range, and scores from different operations or configurations should not be treated as calibrated without inspecting them.

### `Lexical()`

```python
Lexical(query, input_aggregation=None, target_aggregation=None)
```

`query` and the aggregation arguments follow the shared search rules above. Lexical scoring returns `0.0` when there is no match, so `score > 0` is the portable predicate for requiring a match; other numeric thresholds are corpus-dependent. A literal string query supports space-separated alternatives, `AND`, `NOT`, double-quoted phrases, and `term*` prefixes. Entry- and group-derived targets are treated as literal terms. Corpus statistics are dataset-scoped, so narrowing the candidate group changes which entries are returned without redefining their lexical scores.

### `Semantic()`

```python
Semantic(query, input_aggregation=None, target_aggregation=None)
```

`query` and the aggregation arguments follow the shared search rules above. Semantic scoring uses cosine similarity under the selected workspace's configured immutable embedding profile. Provider, model, dimensions, and profile revision are runtime configuration rather than API arguments; unavailable or failed semantic configuration produces a repairable runtime diagnostic.

`Lexical()` and `Semantic()` operation objects expose `.query`, `.input_aggregation`, and `.target_aggregation`.

## Mutations

### Mutation Model

Mutations change only the analysis overlay for the active session and dataset version. A registered source field, source value, entry identity, or dataset version can never be changed through this API. Analysis mutations are staged inside the current execution and become durable only when the entire execution succeeds.

### `create_field()`

```python
create_field(field)
```

`field` is a non-empty string or non-source `Field`. The function returns the resolved analysis `Field`; confirming an existing analysis field is idempotent, while colliding with a source field is an error.

### `tag()`

```python
tag(group, field, value)
```

`group` is an entry-scoped `GroupExpr` or `list[Entry]`; `field` is an analysis `Field`; and `value` is a supported JSON-like value that contains no `None`. The function replaces the selected value for each entry and returns `None`.

### `untag()`

```python
untag(group, field)
untag(group, field, value)
```

`group` is an entry-scoped `GroupExpr` or `list[Entry]`; `field` is an analysis `Field`; and optional `value` is a supported JSON-like value that contains no `None`. Omitting `value` clears every selected value; supplying it clears only exact matches. The function returns `None`.

### Selection and Deletion Semantics

A list passed to `tag()` is stored as one field value rather than expanded. Empty mutation selections are no-ops. Registered fields cannot be deleted: `del field_name` deletes only a Python binding, while `untag()` clears selected analysis values.

### Read-Your-Writes Visibility

After `create_field()`, `tag()`, or `untag()` returns, later reads, predicates, groups, rankings, and search operations in the same execution see the staged state rather than pre-mutation analysis values.

### Session and Dataset-Version Isolation

Analysis fields and tagged values are visible only to the same session and immutable dataset version. Entry handles and member groups from another dataset or version are rejected, and no mutation can cross the singular dataset named by the current `quail_exec` call.

### Symbolic Re-evaluation After Mutations

Symbolic specifications do not freeze analysis-field values when constructed. If a persisted or local expression, predicate, group, or ranking refers to an analysis field, its next materialization evaluates the field's current staged or committed values. Previously materialized Python lists do not update automatically; call `retrieve()` or `count()` again.

### Atomic Rollback

Any later syntax, runtime, persistence, cancellation, timeout, or resource failure rolls back all mutations from the execution and discards its output and binding changes. For a broad mutation, inspect and print the intended bounded selection in a separate successful read-only execution before running the mutation.

## Python Execution Surface

### Supported Statements and Expressions

Quail supports JSON-like scalar, list, and dictionary literals; tuple and set construction for execution-only work; arithmetic on supported concrete values; assignment, reassignment, deletion, and unpacking of variable names; ordinary `if`/`elif`/`else`, `for`, and `while` control flow; `break`, `continue`, and `pass`; comparisons and boolean logic over concrete values; membership tests, subscripting, and slicing; and calls to the injected functions, constructors, built-ins, and methods documented here. Symbolic Quail values use only their documented operators and materialization paths.

### Supported Built-ins

The injected safe built-ins are `abs`, `all`, `any`, `bool`, `dict`, `enumerate`, `float`, `int`, `len`, `list`, `max`, `min`, `range`, `repr`, `round`, `set`, `str`, `sum`, and `tuple`. They retain their ordinary Python call forms within Quail's bounded value and resource limits.

### Supported String and Quail Methods

Supported pure string method call forms are `text.startswith(prefix[, start[, end]])`, `text.endswith(suffix[, start[, end]])`, `text.lower()`, `text.upper()`, `text.casefold()`, `text.strip(chars=None)`, `text.lstrip(chars=None)`, `text.rstrip(chars=None)`, `text.replace(old, new, count=-1)`, `text.split(sep=None, maxsplit=-1)`, `text.rsplit(sep=None, maxsplit=-1)`, `text.splitlines(keepends=False)`, `text.count(sub[, start[, end]])`, `text.find(sub[, start[, end]])`, `text.rfind(sub[, start[, end]])`, `text.removeprefix(prefix)`, and `text.removesuffix(suffix)`.

String-method arguments are positional only; parameter names shown in these signatures describe their positions and are not accepted as keyword arguments.

Supported Quail method call forms are `entry.value(field, default=None)`, `entry.fields()`, `group.where(predicate)`, and `re.escape(pattern)`. Approved methods must be called directly on their receiver and cannot be detached into another binding.

### Reserved Names

Every injected API name, regex name, error class, and safe built-in is reserved and cannot be assigned or deleted. Dangerous ambient names such as `open`, `eval`, `exec`, `compile`, `globals`, `getattr`, and `__import__` are unavailable. User variable names must be valid Python identifiers, cannot start with `__quail_`, and cannot exceed 128 UTF-8 bytes.

### Imports and External Access

Imports, function or class definitions, lambdas, comprehensions, generators, f-strings, exception machinery, assertions, pattern matching, async syntax, context managers, global or nonlocal declarations, assignment expressions, annotated or augmented assignment, identity comparisons with `is` or `is not`, item or attribute assignment, reflective access, and mutating container methods are unavailable. Code has no filesystem, network, process, environment, credential, database, or MCP access. Rebuild and rebind local containers, for example `items = items + [value]` or `mapping = {**mapping, key: value}`.

### Resource Limits

Every execution remains subject to deployment-configured bounds on code, memory, output, API calls, result items, mutation volume, materialized data, bindings, and protocol traffic. Retrieval limits bound returned windows, not all work needed to select, score, or deduplicate candidates. Exceeding any resource limit fails the complete execution atomically with a diagnostic naming the category.

### Cancellation and Time Windows

With `time_window="standard"`, the deployment's normal bounded wall-clock and worker CPU-time limits apply. With `time_window="extended"`, longer deployment-configured wall-clock and worker CPU-time limits apply. Both windows remain finite. Caller cancellation, server shutdown, session serialization, and every non-time resource limit remain effective in both modes; an extended window does not make an unbounded or non-terminating program valid.

## Binding Persistence

### Persisted Names and Types

Quail can persist `None`, strings, booleans, bounded integers, finite floats, recursively persistable lists, JSON-like string-keyed dictionaries, `Field`, `Unit`, operation objects, `Expression`, `Predicate`, `GroupExpr`, `Ranking`, runtime-issued `Entry` handles, and materialized result lists. A dictionary remains JSON-like and therefore cannot contain Quail specifications as values. Persisted values must be acyclic and fit the configured binding count, item, depth, and byte limits.

### Assignment, Reassignment, and Deletion

Successful assignment to a user variable creates or replaces that binding. `del name` removes a persisted binding when the name remains deleted at completion, while deleting and then reassigning the name persists the final value. Names assigned as loop targets or through unpacking are bindings too. Reading a persisted binding without assigning it preserves it unchanged.

### Dataset and Version Scope

Bindings are available only in the same session and immutable dataset version in which they were committed. Reusing the session with another dataset or a later dataset version receives that scope's independent bindings. Persisted entry handles and groups containing entries are rejected if materialized outside their issuing dataset version.

### Symbolic Bindings After Mutations

Persisted symbolic values retain recipes and identities, not cached evaluation results. Expressions, predicates, predicate groups, and rankings therefore use current committed analysis-field values each time they are materialized. Persisted materialized lists retain their saved items and do not update automatically.

### Unsupported Values and Persistence Errors

Tuples, sets, callables, error instances, the injected `re` helper, cycles, non-finite numbers, dictionaries with non-string keys, and other undocumented objects cannot persist. Convert execution-only tuples or sets to lists, rebind unsupported values to a persistable form, or `del` the binding before completion. Quail validates bindings after user code runs; one unsupported assigned value that remains at completion fails and rolls back the complete execution, and the diagnostic identifies the binding and nested value path when available.

## Output

### `print()`

```python
print(*values, sep=" ", end="\n")
```

`sep` and `end` must be strings. The function returns `None` and appends its rendered values joined by `sep` and followed by `end` to one execution-local text buffer. Multiple calls concatenate in call order. The return value of the last expression and unprinted local values are never returned. Successful completion returns the complete buffer as `printed_output`, or `""` when nothing was printed.

### Value Formatting

Formatting is deterministic and avoids process-specific object representations. Top-level strings print without quotes; nested strings use quoted representations; `None`, booleans, bounded integers, and finite floats use stable Python-style text; lists, tuples, and dictionaries retain order; sets are ordered by their rendered items; entries print a concise identity; and public Quail specifications print reconstructible public forms. `str()` and `repr()` use the same formatter with their ordinary top-level string distinction. Unsupported values or dictionaries with non-string keys produce a diagnostic instead of leaking an implementation representation.

### Output Limits

The deployment's output limit applies to the accumulated UTF-8 bytes, including separators and line endings. Crossing it fails the execution rather than truncating the output. Keep results bounded and print only the fields and excerpts the caller needs.

### Discarded Output on Failure

Output is buffered and is not streamed or released before commit. Any later validation, runtime, persistence, timeout, cancellation, or resource failure discards every byte printed by that execution.

## Errors and Diagnostics

### Public Error Classes

| Error class | Meaning |
|---|---|
| `QuailSyntaxError` | Invalid Python or public API shape, unsupported syntax, or invalid symbolic composition. |
| `QuailScopeError` | Incompatible session, dataset version, entry, field, group, or unit scope. |
| `QuailFieldError` | Unknown field, field-kind mismatch, or attempted source-field mutation. |
| `QuailRuntimeError` | Data-dependent evaluation failure, unavailable search, cancellation, timeout, resource exhaustion, or internal execution failure. |

`QuailError` is the common base class. Code inside `quail_exec` cannot catch these errors because exception-handling syntax is unavailable; repair the reported source and rerun the complete call.

### Tool Failure Payload

A failed tool call carries this public shape, possibly inside a client-specific MCP error wrapper:

```text
{
  "execution_id": string | None,
  "diagnostic": {
    "error_class": string,
    "stable_error_code": string,
    "message": string,
    "repair_hint": string | None,
    "source_span": {"line": integer | None, "column": integer | None},
    "redacted_context": object
  }
}
```

`execution_id` identifies the auditable execution when one began and is `None` when authorization, argument validation, or another pre-execution check rejected the request. Branch on `stable_error_code`, use `message` for the specific condition, and follow `repair_hint` before retrying.

### Diagnostic Fields and Source Locations

`error_class` gives the broad public category and `stable_error_code` gives the machine-stable condition. `message` and `repair_hint` are bounded single-line text. `source_span.line` and `.column` are one-based locations in `code` when Quail can attribute the failure; either can be `None` for host-wide or pre-execution failures. `redacted_context` contains only small repair context such as phase, resource category, or operation and never serves as a data-return channel.

### Syntax and Call-Shape Errors

Invalid Python, unavailable constructs, unresolved names, reserved-name assignment, wrong arity or keywords, unsupported attributes or methods, invalid constructors, and invalid symbolic operators report `QuailSyntaxError` with stable code `INVALID_API_SHAPE`. Repair the source at the reported location rather than relying on Python implementation details.

### Scope, Field, and Entry Errors

An unavailable account, session, or dataset rejected before execution reports `NOT_AUTHORIZED` without revealing whether the requested identifier exists. Incompatible entry and field groups, cross-dataset or cross-version handles, invalid unit/group pairings, and other in-execution scope mismatches report `QuailScopeError` with `INVALID_SCOPE`. Unknown fields, explicit kind mismatches, and source-field writes report `QuailFieldError` with `INVALID_FIELD`. Inspect `G1` and retrieve fresh handles in the active scope before retrying.

### Data-Dependent Runtime Errors

Valid symbolic code can fail only when evaluated, for example when `AsNumber()` receives nonnumeric text, `Length()` receives an unsupported shape, numeric ordering encounters a null or nonnumeric value, or output formatting receives an unsupported object. These conditions report `QuailRuntimeError` with `RUNTIME_ERROR`; normalize or filter the data before the failing materialization.

### Regex and Search Errors

Invalid regex syntax, flags, or unsupported constructs fail construction with `INVALID_API_SHAPE`; incompatible field values fail evaluation with `RUNTIME_ERROR`. Missing usable search target text is an API-shape or field-data error depending on when targets resolve. A transient or unavailable search subsystem reports `SEARCH_UNAVAILABLE`; retry the complete execution only after addressing the diagnostic or restoring the configured search service.

### Persistence and Mutation Errors

Unsupported binding shapes report `INVALID_API_SHAPE`, while binding-size or mutation-work bounds report `RESOURCE_LIMIT` with the relevant category. Invalid mutation fields, values, groups, or scopes report their corresponding syntax, field, or scope code. Every such failure occurs before commit, even when user code and earlier mutations had already run.

### Timeout, Cancellation, and Resource Errors

Configured time limits report `EXECUTION_TIMEOUT`, caller or host cancellation reports `EXECUTION_CANCELLED`, and non-time limits report `RESOURCE_LIMIT` with a resource category and operation when known. A concurrent call in the same session can report `SESSION_BUSY`. Internal execution failures report `RUNTIME_CRASHED` or `INTERNAL_ERROR`; follow the repair hint, which may request one retry or an incident report. `time_window="extended"` applies longer deployment-configured execution-time limits than `"standard"`, but it does not prevent timeouts, cancellation, session contention, internal failure, or non-time resource errors.

### Atomic Failure Behavior

Every failure discards staged mutations, changed or deleted bindings, audit records from user operations, and printed output. When an execution began, its failure diagnostic and failed status remain auditable. Rerun the entire repaired execution; never assume an earlier statement from a failed call committed.
