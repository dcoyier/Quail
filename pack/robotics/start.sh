#!/usr/bin/env bash
# Start local Quail against an assembled pack + bundled Ollama embedder.
#
# Usage: start.sh [BASE] [PACK_DIR]
#   BASE     directory with unzipped siblings (default /mnt/data)
#   PACK_DIR optional pack path or basename under BASE; if omitted, pick the
#            unique sibling that contains both quail.toml and assemble.sh
#
# Sibling layout under BASE:
#   <domain>-quail/          pack (quail.toml + assemble.sh)
#   ollama-runtime/
#   embeddinggemma-q8-model/
#   quail-wheel/             (this script’s home)
set -euo pipefail

BASE="${1:-/mnt/data}"
PACK_ARG="${2:-}"

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

is_pack_dir() {
  local d="$1"
  [[ -f "$d/quail.toml" && -f "$d/assemble.sh" ]]
}

# Nested unzip: quail-wheel(N)/quail-wheel/
resolve_nested() {
  local d="$1"
  if [[ -d "$d/quail-wheel" ]]; then
    printf '%s\n' "$d/quail-wheel"
  else
    printf '%s\n' "$d"
  fi
}

find_pack() {
  local base="$1"
  local -a hits=()
  local d name
  while IFS= read -r -d '' d; do
    name="$(basename "$d")"
    case "$name" in
      ollama-runtime|ollama-runtime\(*\)|embeddinggemma-q8-model|embeddinggemma-q8-model\(*\)|quail-wheel|quail-wheel\(*\))
        continue
        ;;
    esac
    # Prefer inner pack if zip nested as name(N)/name/
    if is_pack_dir "$d"; then
      hits+=("$d")
    else
      for inner in "$d"/*; do
        [[ -d "$inner" ]] || continue
        if is_pack_dir "$inner"; then
          hits+=("$inner")
        fi
      done
    fi
  done < <(find "$base" -maxdepth 1 -mindepth 1 -type d -print0 2>/dev/null)

  if [[ ${#hits[@]} -eq 1 ]]; then
    printf '%s\n' "${hits[0]}"
    return 0
  fi
  if [[ ${#hits[@]} -eq 0 ]]; then
    return 1
  fi
  echo "multiple pack dirs under $base; pass PACK_DIR as second arg:" >&2
  printf '  %s\n' "${hits[@]}" >&2
  return 1
}

resolve_pack() {
  local base="$1" arg="$2"
  if [[ -n "$arg" ]]; then
    if [[ -d "$arg" ]] && is_pack_dir "$arg"; then
      printf '%s\n' "$(cd "$arg" && pwd)"
      return 0
    fi
    if [[ -d "$base/$arg" ]] && is_pack_dir "$base/$arg"; then
      printf '%s\n' "$(cd "$base/$arg" && pwd)"
      return 0
    fi
    # ChatGPT name(N) suffix
    local candidate
    candidate="$(find "$base" -maxdepth 1 -type d -name "${arg}(*)" 2>/dev/null | sort -V | tail -1 || true)"
    if [[ -n "${candidate:-}" ]] && is_pack_dir "$candidate"; then
      printf '%s\n' "$(cd "$candidate" && pwd)"
      return 0
    fi
    if [[ -n "${candidate:-}" ]]; then
      for inner in "$candidate"/*; do
        if [[ -d "$inner" ]] && is_pack_dir "$inner"; then
          printf '%s\n' "$(cd "$inner" && pwd)"
          return 0
        fi
      done
    fi
    echo "not a pack dir (need quail.toml + assemble.sh): $arg" >&2
    return 1
  fi
  find_pack "$base"
}

PACK="$(resolve_pack "$BASE" "$PACK_ARG" || true)"
OLLAMA_RT="$(pick_dir "$BASE" ollama-runtime || true)"
MODEL="$(pick_dir "$BASE" embeddinggemma-q8-model || true)"
WHEEL_DIR="$(pick_dir "$BASE" quail-wheel || true)"

echo "BASE=$BASE"
echo "PACK=${PACK:-MISSING}"
echo "OLLAMA_RT=${OLLAMA_RT:-MISSING}"
echo "MODEL=${MODEL:-MISSING}"
echo "WHEEL_DIR=${WHEEL_DIR:-MISSING}"

if [[ -z "${PACK:-}" || ! -d "$PACK" ]]; then
  echo "missing pack under $BASE (dir with quail.toml + assemble.sh)" >&2
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

if [[ -n "${WHEEL_DIR:-}" ]]; then
  WHEEL_DIR="$(resolve_nested "$WHEEL_DIR")"
fi

echo "== assemble pack =="
(cd "$PACK" && bash assemble.sh)

echo "== install quail =="
if ! python3 -c "import quail" >/dev/null 2>&1; then
  if [[ -z "${WHEEL_DIR:-}" || ! -d "$WHEEL_DIR" ]]; then
    echo "quail not importable and missing wheel dir" >&2
    exit 2
  fi
  WHL=$(ls "$WHEEL_DIR"/quail-*.whl | head -1)
  DEPS="$WHEEL_DIR/deps"
  echo "python=$(python3 -V 2>&1) whl=$WHL"
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
python3 -c "import quail; print('quail', getattr(quail, '__version__', '?'))"

echo "== start ollama =="
export OLLAMA_HOST=127.0.0.1:11434
bash "$OLLAMA_RT/run_ollama.sh" "$MODEL/models"

PACK_ABS="$(cd "$PACK" && pwd)"
CONFIG="$PACK_ABS/quail.toml"
QUAIL_LOG="${QUAIL_RUN_LOG:-/tmp/quail-run.log}"

echo "== start quail run =="
code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://127.0.0.1:8000/mcp" || true)"
if [[ -n "$code" && "$code" != "000" ]]; then
  echo "MCP already listening on :8000 (http=$code)"
else
  if [[ -f /tmp/quail-run.pid ]]; then
    oldpid="$(cat /tmp/quail-run.pid || true)"
    if [[ -n "${oldpid:-}" ]] && kill -0 "$oldpid" 2>/dev/null; then
      kill "$oldpid" 2>/dev/null || true
      sleep 1
    fi
    rm -f /tmp/quail-run.pid
  fi
  : >"$QUAIL_LOG"
  nohup quail run --config "$CONFIG" >"$QUAIL_LOG" 2>&1 &
  echo $! >/tmp/quail-run.pid
  echo "quail_run_pid=$(cat /tmp/quail-run.pid)"
  ready=0
  for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://127.0.0.1:8000/mcp" || true)"
    if [[ -n "$code" && "$code" != "000" ]]; then
      echo "MCP listening http=$code"
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

echo "READY mcp=http://127.0.0.1:8000/mcp config=$CONFIG"
