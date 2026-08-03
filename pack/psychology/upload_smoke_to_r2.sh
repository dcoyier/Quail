#!/usr/bin/env bash
# Upload the Quail R2 smoke zip to Cloudflare R2 (S3-compatible API).
# Requires: rclone OR aws CLI + curl, and env vars below.
set -euo pipefail

ZIP="${1:-/tmp/quail-smoke-r2.zip}"
KEY="${R2_OBJECT_KEY:-quail-smoke/quail-smoke-r2.zip}"

: "${CF_ACCOUNT_ID:?set CF_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"
: "${R2_BUCKET:?set R2_BUCKET}"

ENDPOINT="https://${CF_ACCOUNT_ID}.r2.cloudflarestorage.com"
PUBLIC_BASE="${R2_PUBLIC_BASE_URL:-}"  # e.g. https://pub-xxxxx.r2.dev or custom domain

if ! command -v aws >/dev/null 2>&1; then
  echo "installing aws cli v2 via pip..." >&2
  pip install -q awscli
fi

export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="auto"

aws s3 cp "$ZIP" "s3://${R2_BUCKET}/${KEY}" \
  --endpoint-url "$ENDPOINT" \
  --content-type application/zip

echo "uploaded s3://${R2_BUCKET}/${KEY}"

if [[ -n "$PUBLIC_BASE" ]]; then
  url="${PUBLIC_BASE%/}/${KEY}"
  echo "PUBLIC_URL=$url"
  echo "---"
  echo "PUBLIC_URL=$url"
  echo "Note: ChatGPT Agent cannot container.download zips; use CU to Library-upload, then attach."
else
  echo "Set R2_PUBLIC_BASE_URL to your r2.dev or custom public base to print the URL."
fi
