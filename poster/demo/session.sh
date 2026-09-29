#!/usr/bin/env bash
# Reproduces the session shown in panel 7 of the poster.
# Needs an active Quail environment and Ollama serving embeddinggemma.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
study="${1:-$(mktemp -d)/demo}"

quail init "$study"
cp "$here/survey.csv" "$study/"
cd "$study"
quail import survey.csv --embed ollama/embeddinggemma --embed-revision v1

quail exec poster -c 'body = Field("body")
kw  = body.lexical("parking") > 0
sem = body.semantic("no place to park near the building") > 0.52
count(kw), count(sem), count(kw & sem)'

quail exec poster -c 'for e in retrieve(kw & ~sem):
    print("keyword only", e.id, e["body"][:44])
for e in retrieve(sem & ~kw):
    print("meaning only", e.id, e["body"][:44])'

quail exec poster -c 'tag(kw | sem, "topic", "parking")
tag(Field("id").isin(["r04", "r13"]), "topic", None)
count(where=Field("topic") == "parking", by=Field("dept"))'

quail exec poster --reset
quail exec poster -c 'count(by=Field("topic"))'
quail exec poster -c 'kw' || true
quail exec poster --close
