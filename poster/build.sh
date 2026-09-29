#!/usr/bin/env bash
# Renders the poster to PDF and PNG, and each figure to its own PNG.
# Usage: poster/build.sh [OUT_DIR]   (default: poster/build)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/build}"
chrome="${CHROME:-$(command -v google-chrome-stable || command -v google-chrome || command -v chromium)}"
flags=(--headless=new --no-sandbox --disable-gpu --hide-scrollbars --no-first-run)
mkdir -p "$out/figures"

render_png() {  # file width height scale output
  timeout 60 "$chrome" "${flags[@]}" --force-device-scale-factor="$4" \
    --window-size="$2,$3" --screenshot="$5" "file://$1" >/dev/null 2>&1
}

for svg in "$here"/figures/*.svg; do
  name="$(basename "$svg" .svg)"
  [ "$name" = qr ] && continue
  read -r w h < <(sed -n 's/.*viewBox="0 0 \([0-9]*\) \([0-9]*\)".*/\1 \2/p' "$svg" | head -1)
  render_png "$svg" "$w" "$h" 2 "$out/figures/$name.png"
done

# The page is 44 x 44 in at 96 px/in; the PNG preview is half scale.
timeout 120 "$chrome" "${flags[@]}" --no-pdf-header-footer \
  --print-to-pdf="$out/poster.pdf" "file://$here/poster.html" >/dev/null 2>&1
render_png "$here/poster.html" 4224 4224 0.5 "$out/poster-preview.png"
echo "Wrote $out"
