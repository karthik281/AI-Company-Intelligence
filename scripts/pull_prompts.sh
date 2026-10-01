#!/usr/bin/env bash
# Copy the prompt files from S3 into prompts/ (run once, and whenever
# someone edits prompts directly in S3).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a; source "$ROOT/config.env"; set +a

aws s3 sync "s3://$S3_BUCKET/prompts/" "$ROOT/prompts/" \
  --region "$S3_REGION" --exclude "*" --include "*.md"

ls -l "$ROOT/prompts"
