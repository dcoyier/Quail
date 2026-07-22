# Quail v0.11

Inspectable qualitative analysis over private corpora (rebuild in progress).

This tree is intentionally small. Product decisions live in
[`docs/BOUNDARY.md`](docs/BOUNDARY.md). The model-facing analysis contract is
[`docs/api.md`](docs/api.md). The previous implementation at
`../Quail v0.10` is reference-only.

## Status

Scaffold plus analysis API contract. Facade implementation is partial; grow it
to match `docs/api.md`.

## Intended operator path (not implemented)

```sh
quail run --config /absolute/path/to/quail.toml
```
