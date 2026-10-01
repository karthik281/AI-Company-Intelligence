#!/usr/bin/env bash
# Copy config/recipients.txt from S3 (run once, and whenever someone
# edits the recipient list directly in S3).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a; source "$ROOT/config.env"; set +a

mkdir -p "$ROOT/config"

aws s3 cp "s3://$S3_BUCKET/config/recipients.txt" \
  "$ROOT/config/recipients.txt" --region "$S3_REGION"

cat "$ROOT/config/recipients.txt"
