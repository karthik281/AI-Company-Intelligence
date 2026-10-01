#!/usr/bin/env bash
# Upload prompts/ to S3. The orchestrator reads prompts from S3 at run
# time, so prompt changes go live without redeploying the Lambda.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a; source "$ROOT/config.env"; set +a

aws s3 sync "$ROOT/prompts/" "s3://$S3_BUCKET/prompts/" \
  --region "$S3_REGION" --exclude "*" --include "*.md" \
  --exclude "README.md"
