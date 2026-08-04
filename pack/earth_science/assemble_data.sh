#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}
assemble_parts() {
  local stem="$1"
  local parts=(${stem}.part*)
  if [[ ${#parts[@]} -eq 0 || ! -f ${parts[0]} ]]; then
    echo "missing ${stem}.part*" >&2
    exit 1
  fi
  echo "assembling ${stem} from ${#parts[@]} parts..."
  cat ${stem}.part* > "$stem"
  expected="$(tr -d '[:space:]' < ${stem}.sha256)"
  actual="$(hash_file "$stem")"
  if [[ "$actual" != "$expected" ]]; then
    echo "checksum mismatch for $stem" >&2
    exit 1
  fi
  echo "ok  $stem  sha256=$actual"
}
assemble_parts data/quail.turso
assemble_parts data/quail-search.turso
echo "ready: quail run --config \"\$(pwd)/quail.toml\""
