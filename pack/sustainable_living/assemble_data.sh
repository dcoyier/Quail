#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"

assemble_parts() {
  local stem="$1"
  local parts=(${stem}.part*)
  if [[ ${#parts[@]} -eq 0 || ! -f ${parts[0]} ]]; then
    echo "missing ${stem}.part* under $root" >&2
    exit 1
  fi
  echo "assembling ${stem} from ${#parts[@]} parts..."
  cat ${stem}.part* > "$stem"
  expected="$(tr -d '[:space:]' < ${stem}.sha256)"
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$stem" | awk '{print $1}')"
  else
    actual="$(shasum -a 256 "$stem" | awk '{print $1}')"
  fi
  if [[ "$actual" != "$expected" ]]; then
    echo "checksum mismatch for $stem" >&2
    exit 1
  fi
  echo "ok  $stem  sha256=$actual"
}

assemble_parts data/quail.turso
assemble_parts data/quail-search.turso
echo "ready: quail run --config \"\$(pwd)/quail.toml\""
