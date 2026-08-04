#!/usr/bin/env bash
# Install the sealed Quail wheel, assemble its data, and launch MCP.
set -euo pipefail
cd "$(dirname "$0")"

wheels=(./quail-*.whl)
if [[ ${#wheels[@]} -ne 1 || ! -f ${wheels[0]} ]]; then
  echo "expected exactly one ./quail-*.whl" >&2
  exit 1
fi

python -m pip install --no-index --find-links=./deps "${wheels[0]}"
bash assemble.sh
exec quail run --config "$(pwd)/quail.toml"
