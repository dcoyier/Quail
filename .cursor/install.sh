#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for Quail.
# Installs uv, syncs the locked dev environment, and installs the example
# connector editable so the connector regression tests import it.
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

# Editable install of the bundled example connector (used by test_connectors.py
# and the connector-SDK docs flow).
uv pip install -e ./examples/notes-connector

echo "Quail environment ready."
