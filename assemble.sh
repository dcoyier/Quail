#!/usr/bin/env bash
# Assemble chunked search Turso for this sealed dataset.
set -euo pipefail
cd "$(dirname "$0")"
hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}
parts=(data/quail-search.turso.part*)
if [[ ${#parts[@]} -eq 0 || ! -f ${parts[0]} ]]; then
  echo "missing data/quail-search.turso.part*" >&2
  exit 1
fi
echo "assembling data/quail-search.turso from ${#parts[@]} parts..."
cat data/quail-search.turso.part* > data/quail-search.turso
expected="$(tr -d '[:space:]' < data/quail-search.turso.sha256)"
actual="$(hash_file data/quail-search.turso)"
if [[ "$actual" != "$expected" ]]; then
  echo "checksum mismatch for data/quail-search.turso" >&2
  exit 1
fi
echo "ok  data/quail-search.turso  sha256=$actual"
echo "ASSEMBLE_OK files=data/quail.turso,data/quail-search.turso,data/articles.csv"
echo "ready: quail run --config \"\$(pwd)/quail.toml\""
