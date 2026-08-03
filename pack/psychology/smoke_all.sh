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
# Prefer nested quail-wheel/ if ChatGPT unzipped zip-into-folder.
if [[ -n "${WHEEL_DIR:-}" && -d "$WHEEL_DIR/quail-wheel" ]]; then
  WHEEL_DIR="$WHEEL_DIR/quail-wheel"
fi
if ! python3 -c "import quail" >/dev/null 2>&1; then
  if [[ -z "${WHEEL_DIR:-}" || ! -d "$WHEEL_DIR" ]]; then
    echo "quail not importable and missing wheel dir" >&2
    exit 2
  fi
  WHL=$(ls "$WHEEL_DIR"/quail-*.whl | head -1)
  DEPS="$WHEEL_DIR/deps"
  echo "python=$(python3 -V 2>&1)"
  echo "whl=$WHL"
  echo "deps=$DEPS"
  if python3 -m pip --version >/dev/null 2>&1; then
    python3 -m pip install -q --upgrade pip
    if [[ -d "$DEPS" ]]; then
      python3 -m pip install -q --no-index --find-links "$DEPS" "$WHL"
    else
      python3 -m pip install -q "$WHL"
    fi
  elif command -v uv >/dev/null 2>&1; then
    if [[ -d "$DEPS" ]]; then
      uv pip install --system -q --no-index --find-links "$DEPS" "$WHL"
    else
      uv pip install --system -q "$WHL"
    fi
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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Prefer scripts beside this file; fall back to copies inside quail-wheel/.
resolve_script() {
  local name="$1"
  if [[ -f "$SCRIPT_DIR/$name" ]]; then
    printf '%s\n' "$SCRIPT_DIR/$name"
  elif [[ -n "${WHEEL_DIR:-}" && -f "$WHEEL_DIR/$name" ]]; then
    printf '%s\n' "$WHEEL_DIR/$name"
  else
    return 1
  fi
}

PACK_ABS="$(cd "$PACK" && pwd)"
CONFIG="$PACK_ABS/quail.toml"
QUAIL_LOG="${QUAIL_RUN_LOG:-/tmp/quail-run-smoke.log}"

# Detect an already-running MCP server before opening the DBs in-process.
# Turso locking: in-process smoke_semantic + live `quail run` cannot share the
# pack DB (ChatGPT Agent hit Failed locking file on re-run).
reuse=0
code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://127.0.0.1:8000/mcp" || true)"
if [[ -n "$code" && "$code" != "000" ]]; then
  echo "reusing existing MCP listener on :8000 (http=$code)"
  reuse=1
fi

if [[ "$reuse" -eq 0 ]]; then
  echo "== in-process semantic smoke (exec_script) =="
  SMOKE_PY="$(resolve_script smoke_semantic.py)"
  python3 "$SMOKE_PY" --config "$CONFIG"

  echo "== start quail run (unrestricted MCP on :8000) =="
  # Stop a leftover smoke server if we own the pid file.
  if [[ -f /tmp/quail-run-smoke.pid ]]; then
    oldpid="$(cat /tmp/quail-run-smoke.pid || true)"
    if [[ -n "${oldpid:-}" ]] && kill -0 "$oldpid" 2>/dev/null; then
      kill "$oldpid" 2>/dev/null || true
      sleep 1
    fi
    rm -f /tmp/quail-run-smoke.pid
  fi
  : >"$QUAIL_LOG"
  nohup quail run --config "$CONFIG" >"$QUAIL_LOG" 2>&1 &
  echo $! >/tmp/quail-run-smoke.pid
  echo "quail_run_pid=$(cat /tmp/quail-run-smoke.pid)"
  # Wait until the MCP route answers (406 Not Acceptable is fine for bare GET).
  ready=0
  for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://127.0.0.1:8000/mcp" || true)"
    if [[ -n "$code" && "$code" != "000" ]]; then
      echo "MCP_LISTEN_OK http=$code"
      ready=1
      break
    fi
    sleep 0.5
  done
  if [[ "$ready" -ne 1 ]]; then
    echo "quail run failed to listen on :8000" >&2
    tail -n 80 "$QUAIL_LOG" >&2 || true
    exit 1
  fi
else
  echo "== skip in-process semantic smoke (MCP already owns pack DB) =="
fi

echo "== MCP quail_setup + quail_exec Semantic smoke =="
SMOKE_MCP="$(resolve_script smoke_mcp.py)"
python3 "$SMOKE_MCP" --url "http://127.0.0.1:8000/mcp"
echo "SMOKE_ALL_OK"
echo "MCP remains at http://127.0.0.1:8000/mcp (quail run)."
echo "For eval: leave it up; call quail_setup then quail_exec for the one question."
