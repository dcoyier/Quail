#!/usr/bin/env bash
# Boot Quail + Ollama for a real ChatGPT Agent eval trial.
# No canned retrieval queries. No OPS_HANDOFF. Leaves MCP up for the question.
#
# Expects sibling unzipped dirs under BASE (default /mnt/data):
#   psychology-base-quail/   (full pack — required for eval quality)
#   ollama-runtime/
#   embeddinggemma-q8-model/
#   quail-wheel/
set -euo pipefail

BASE="${1:-/mnt/data}"

pick_dir() {
  local base="$1" stem="$2"
  local candidate
  if [[ -d "$base/$stem" ]]; then
    printf '%s\n' "$base/$stem"
    return 0
  fi
  candidate="$(find "$base" -maxdepth 1 -type d -name "${stem}(*)" 2>/dev/null | sort -V | tail -1 || true)"
  if [[ -n "${candidate:-}" && -d "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  return 1
}

PACK="$(pick_dir "$BASE" psychology-base-quail || true)"
OLLAMA_RT="$(pick_dir "$BASE" ollama-runtime || true)"
MODEL="$(pick_dir "$BASE" embeddinggemma-q8-model || true)"
WHEEL_DIR="$(pick_dir "$BASE" quail-wheel || true)"

echo "EVAL_BASE=$BASE"
echo "PACK=${PACK:-MISSING}"
echo "OLLAMA_RT=${OLLAMA_RT:-MISSING}"
echo "MODEL=${MODEL:-MISSING}"
echo "WHEEL_DIR=${WHEEL_DIR:-MISSING}"

if [[ -z "${PACK:-}" || ! -d "$PACK" ]]; then
  echo "missing psychology-base-quail under $BASE (full pack required for eval)" >&2
  ls -la "$BASE" >&2 || true
  exit 2
fi
# Refuse mini pack for eval boot — wrong retrieval quality.
case "$(basename "$PACK")" in
  psychology-mini-quail|psychology-mini-quail\(*\))
    echo "refusing psychology-mini-quail for eval; attach psychology-base-quail.zip" >&2
    exit 2
    ;;
esac
if [[ -z "${OLLAMA_RT:-}" || ! -d "$OLLAMA_RT" ]]; then
  echo "missing ollama-runtime under $BASE" >&2
  exit 2
fi
if [[ -z "${MODEL:-}" || ! -d "$MODEL" ]]; then
  echo "missing embeddinggemma-q8-model under $BASE" >&2
  exit 2
fi

if [[ -n "${WHEEL_DIR:-}" && -d "$WHEEL_DIR/quail-wheel" ]]; then
  WHEEL_DIR="$WHEEL_DIR/quail-wheel"
fi

echo "== assemble pack =="
(cd "$PACK" && bash assemble.sh)

echo "== install quail wheel =="
if ! python3 -c "import quail" >/dev/null 2>&1; then
  if [[ -z "${WHEEL_DIR:-}" || ! -d "$WHEEL_DIR" ]]; then
    echo "quail not importable and missing wheel dir" >&2
    exit 2
  fi
  WHL=$(ls "$WHEEL_DIR"/quail-*.whl | head -1)
  DEPS="$WHEEL_DIR/deps"
  echo "python=$(python3 -V 2>&1)"
  echo "whl=$WHL"
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

echo "== check embed model listed =="
python3 - <<'PY'
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=30) as resp:
    body = json.loads(resp.read())
names = [m.get("name", "") for m in body.get("models") or []]
need = "embeddinggemma:300m-qat-q8_0"
assert any(need in n for n in names), names
print("OLLAMA_MODEL_OK", need)
PY

PACK_ABS="$(cd "$PACK" && pwd)"
CONFIG="$PACK_ABS/quail.toml"
QUAIL_LOG="${QUAIL_RUN_LOG:-/tmp/quail-run-eval.log}"

echo "== start quail run (MCP :8000) =="
reuse=0
code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://127.0.0.1:8000/mcp" || true)"
if [[ -n "$code" && "$code" != "000" ]]; then
  echo "reusing existing MCP listener on :8000 (http=$code)"
  reuse=1
fi
if [[ "$reuse" -eq 0 ]]; then
  if [[ -f /tmp/quail-run-eval.pid ]]; then
    oldpid="$(cat /tmp/quail-run-eval.pid || true)"
    if [[ -n "${oldpid:-}" ]] && kill -0 "$oldpid" 2>/dev/null; then
      kill "$oldpid" 2>/dev/null || true
      sleep 1
    fi
    rm -f /tmp/quail-run-eval.pid
  fi
  : >"$QUAIL_LOG"
  nohup quail run --config "$CONFIG" >"$QUAIL_LOG" 2>&1 &
  echo $! >/tmp/quail-run-eval.pid
  echo "quail_run_pid=$(cat /tmp/quail-run-eval.pid)"
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
fi

echo "EVAL_READY mcp=http://127.0.0.1:8000/mcp config=$CONFIG"
echo "Next: quail_setup once, then quail_exec for the ONE eval question. Do not browse the web."
