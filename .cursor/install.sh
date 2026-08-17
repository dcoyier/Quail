#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for Quail.
# Installs uv and syncs the locked dev environment.
set -euo pipefail

# Ensure a user-local bin (where uv installs) is on PATH for this script.
export PATH="$HOME/.local/bin:$PATH"
# shellcheck disable=SC1091
[ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  # shellcheck disable=SC1091
  [ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"
fi

echo "uv: $(uv --version)"

# Locked dependency sync (creates .venv from uv.lock) plus dev tools.
uv sync --group dev

echo "Quail environment ready."
