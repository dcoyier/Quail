#!/usr/bin/env bash
# Reproduces the example session on the poster.
# Needs an active Quail environment and Ollama serving embeddinggemma.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
study="${1:-$(mktemp -d)/demo}"

quail init "$study"
cp "$here/survey.csv" "$study/"
cd "$study"
quail import survey.csv --embed ollama/embeddinggemma --embed-revision v1

quail exec first-pass -c 'body = Field("body")
word = body.lexical("parking") > 0
for e in retrieve(word):
    print(e.id, e["body"][:58])'

quail exec first-pass -c 'meaning = body.semantic("no place to park near the building") > 0.52
for e in retrieve(meaning & ~word):
    print(e.id, e["body"][:58])'

quail exec first-pass -c 'tag(word | meaning, "topic", "parking")
tag(Field("id").isin(["r04", "r13"]), "topic", None)
count(where=Field("topic") == "parking", by=Field("dept"))'

quail exec first-pass --reset
quail exec first-pass -c 'count(by=Field("topic"))'
quail exec first-pass -c 'word' || true
quail exec first-pass --close
