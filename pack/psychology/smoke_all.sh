#!/usr/bin/env bash
# ChatGPT Agent end-to-end smoke for psychology base Quail + bundled Ollama.
# Expects sibling unzipped dirs under BASE (default /mnt/data):
#   psychology-base-quail/
#   ollama-runtime/
#   embeddinggemma-q8-model/
#   quail-wheel/   (optional if quail already on PATH)
set -euo pipefail

BASE="${1:-/mnt/data}"

# ChatGPT often unzips as "name(1)/" when the stem already exists — accept that.
pick_dir() {
  local base="$1" stem="$2"
  local candidate
  if [[ -d "$base/$stem" ]]; then
    printf '%s\n' "$base/$stem"
    return 0
  fi
  # Prefer highest numeric suffix if several copies exist.
  # Avoid pipefail trip when the glob matches nothing.
  candidate="$(find "$base" -maxdepth 1 -type d -name "${stem}(*)" 2>/dev/null | sort -V | tail -1 || true)"
  if [[ -n "${candidate:-}" && -d "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  return 1
}

PACK="$(pick_dir "$BASE" psychology-mini-quail || pick_dir "$BASE" psychology-base-quail || true)"
OLLAMA_RT="$(pick_dir "$BASE" ollama-runtime || true)"
MODEL="$(pick_dir "$BASE" embeddinggemma-q8-model || true)"
WHEEL_DIR="$(pick_dir "$BASE" quail-wheel || true)"

echo "SMOKE_BASE=$BASE"
echo "PACK=${PACK:-MISSING}"
echo "OLLAMA_RT=${OLLAMA_RT:-MISSING}"
echo "MODEL=${MODEL:-MISSING}"
echo "WHEEL_DIR=${WHEEL_DIR:-MISSING}"

if [[ -z "${PACK:-}" || ! -d "$PACK" ]]; then
  echo "missing psychology-mini-quail or psychology-base-quail under $BASE" >&2
  ls -la "$BASE" >&2 || true
  exit 2
fi
if [[ -z "${OLLAMA_RT:-}" || ! -d "$OLLAMA_RT" ]]; then
  echo "missing ollama-runtime under $BASE" >&2
  exit 2
fi
if [[ -z "${MODEL:-}" || ! -d "$MODEL" ]]; then
  echo "missing embeddinggemma-q8-model under $BASE" >&2
  exit 2
fi

echo "== assemble pack =="
(cd "$PACK" && bash assemble.sh)

echo "== install quail wheel (if present) =="
if ! python3 -c "import quail" >/dev/null 2>&1; then
  if [[ ! -d "$WHEEL_DIR" ]]; then
    echo "quail not importable and missing $WHEEL_DIR" >&2
    exit 2
  fi
  WHL=$(ls "$WHEEL_DIR"/quail-*.whl | head -1)
  if python3 -m pip --version >/dev/null 2>&1; then
    python3 -m pip install -q --upgrade pip
    python3 -m pip install -q "$WHL"
  elif command -v uv >/dev/null 2>&1; then
    uv pip install --system -q "$WHL"
  else
    echo "need pip or uv to install $WHL" >&2
    exit 2
  fi
fi
python3 -c "import quail; print('quail_import_ok', getattr(quail, '__version__', '?'))"

echo "== start ollama =="
export OLLAMA_HOST=127.0.0.1:11434
bash "$OLLAMA_RT/run_ollama.sh" "$MODEL/models"

echo "== verify embed API =="
python3 - <<'PY'
import json, urllib.request
payload = json.dumps({
    "model": "embeddinggemma:300m-qat-q8_0",
    "input": ["cognitive dissonance"],
    "dimensions": 768,
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:11434/api/embed",
    data=payload,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=600) as resp:
    body = json.loads(resp.read())
emb = body.get("embeddings")
assert isinstance(emb, list) and len(emb) == 1 and len(emb[0]) == 768, body
print("EMBED_API_OK dim=768")
PY

echo "== semantic retrieve smoke =="
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SMOKE_PY="$SCRIPT_DIR/smoke_semantic.py"
if [[ ! -f "$SMOKE_PY" ]]; then
  # Also accept copy shipped inside quail-wheel/
  SMOKE_PY="$WHEEL_DIR/smoke_semantic.py"
fi
python3 "$SMOKE_PY" --config "$(cd "$PACK" && pwd)/quail.toml"
echo "SMOKE_ALL_OK"
