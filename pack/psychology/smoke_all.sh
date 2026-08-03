#!/usr/bin/env bash
# ChatGPT Agent end-to-end smoke for psychology base Quail + bundled Ollama.
# Expects sibling unzipped dirs under BASE (default /mnt/data):
#   psychology-base-quail/
#   ollama-runtime/
#   embeddinggemma-q8-model/
#   quail-wheel/   (optional if quail already on PATH)
set -euo pipefail

BASE="${1:-/mnt/data}"
PACK="$BASE/psychology-base-quail"
OLLAMA_RT="$BASE/ollama-runtime"
MODEL="$BASE/embeddinggemma-q8-model"
WHEEL_DIR="$BASE/quail-wheel"

echo "SMOKE_BASE=$BASE"

if [[ ! -d "$PACK" ]]; then
  echo "missing $PACK — unzip psychology-base-quail.zip first" >&2
  exit 2
fi
if [[ ! -d "$OLLAMA_RT" ]]; then
  echo "missing $OLLAMA_RT — unzip ollama-runtime.zip first" >&2
  exit 2
fi
if [[ ! -d "$MODEL" ]]; then
  echo "missing $MODEL — unzip embeddinggemma-q8-model.zip first" >&2
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
