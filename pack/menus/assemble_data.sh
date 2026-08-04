#!/usr/bin/env bash
# Assemble chunked menu databases and verify their SHA-256 receipts.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"

hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

assemble_database() {
  local stem="$1"
  local -a parts=("${stem}.part"*)
  if [[ ${#parts[@]} -eq 0 || ! -f "${parts[0]}" ]]; then
    echo "missing ${stem}.part*" >&2
    exit 1
  fi

  local expected actual temporary
  expected="$(tr -d '[:space:]' <"${stem}.sha256")"
  temporary="${stem}.assembling"
  rm -f "$temporary"
  cat "${parts[@]}" >"$temporary"
  actual="$(hash_file "$temporary")"
  if [[ "$actual" != "$expected" ]]; then
    rm -f "$temporary"
    echo "checksum mismatch for $stem" >&2
    exit 1
  fi
  mv "$temporary" "$stem"
  echo "assembled $stem parts=${#parts[@]} sha256=$actual"
}

assemble_database data/quail.turso
assemble_database data/quail-search.turso
echo "ASSEMBLE_OK"
