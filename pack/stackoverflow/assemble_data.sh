#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"

csv_parts=(data/articles.csv.part*)
if [[ ${#csv_parts[@]} -eq 0 || ! -f ${csv_parts[0]} ]]; then
  echo "missing data/articles.csv.part* under $root" >&2
  exit 1
fi
echo "assembling data/articles.csv from ${#csv_parts[@]} parts..."
# part00 has header; later parts also have header — skip duplicate headers
{
  cat "${csv_parts[0]}"
  for part in "${csv_parts[@]:1}"; do
    tail -n +2 "$part"
  done
} > data/articles.csv
echo "ok  data/articles.csv"

parts=(data/quail-search.turso.part*)
if [[ ${#parts[@]} -eq 0 || ! -f ${parts[0]} ]]; then
  echo "note: no search DB parts yet (warm not sealed)" >&2
else
  echo "assembling data/quail-search.turso from ${#parts[@]} parts..."
  cat data/quail-search.turso.part* > data/quail-search.turso
  expected="$(tr -d '[:space:]' < data/quail-search.turso.sha256)"
  actual="$(sha256sum data/quail-search.turso | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    echo "checksum mismatch for data/quail-search.turso" >&2
    exit 1
  fi
  echo "ok  data/quail-search.turso  sha256=$actual"
fi
echo "ready: quail run --config \"\$(pwd)/quail.toml\""
