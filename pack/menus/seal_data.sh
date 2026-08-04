#!/usr/bin/env bash
# Seal warmed menu databases into normal-git-sized chunks (no Git LFS).
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

hash_stream() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  else
    shasum -a 256 | awk '{print $1}'
  fi
}

seal_database() {
  local stem="$1"
  if [[ ! -f "$stem" ]]; then
    echo "missing warmed database: $stem" >&2
    exit 1
  fi
  if [[ -s "${stem}-wal" ]]; then
    echo "refusing to seal nonempty WAL: ${stem}-wal" >&2
    exit 1
  fi

  rm -f "${stem}.part"* "${stem}.sha256"
  split -b 80m -d -a 2 "$stem" "${stem}.part"
  hash_file "$stem" >"${stem}.sha256"

  local expected actual count
  local -a parts=("${stem}.part"*)
  expected="$(tr -d '[:space:]' <"${stem}.sha256")"
  actual="$(cat "${parts[@]}" | hash_stream)"
  count="${#parts[@]}"
  if [[ "$actual" != "$expected" ]]; then
    echo "chunk verification failed for $stem" >&2
    exit 1
  fi
  echo "sealed $stem parts=$count sha256=$expected"
}

seal_database data/quail.turso
seal_database data/quail-search.turso
echo "SEAL_OK"
