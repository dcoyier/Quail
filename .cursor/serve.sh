#!/usr/bin/env bash
# Bring up the Quail MCP server for the bundled unrestricted example.
# `quail run` fail-closes unless each imported version is already active and
# warm, so `quail process` runs first to import and activate the example CSV.
# The CLI requires an absolute --config path, so resolve it from the repo root.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
# shellcheck disable=SC1091
[ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$REPO_ROOT/examples/quail.toml"

cd "$REPO_ROOT"
uv run quail process --config "$CONFIG"
exec uv run quail run --config "$CONFIG"
