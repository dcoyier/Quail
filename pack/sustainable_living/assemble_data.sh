#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
parts=(data/quail-search.turso.part*)
if [[ ${#parts[@]} -eq 0 || ! -f ${parts[0]} ]]; then
  echo "missing data/quail-search.turso.part* under $root" >&2
  exit 1
fi
echo "assembling data/quail-search.turso from ${#parts[@]} parts..."
cat data/quail-search.turso.part* > data/quail-search.turso
expected="$(tr -d '[:space:]' < data/quail-search.turso.sha256)"
actual="$(sha256sum data/quail-search.turso | awk '{print $1}')"
if [[ "$actual" != "$expected" ]]; then
  echo "checksum mismatch for data/quail-search.turso" >&2
  exit 1
fi
echo "ok  data/quail-search.turso  sha256=$actual"
echo "ready: quail run --config \"\$(pwd)/quail.toml\""
