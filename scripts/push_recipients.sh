#!/usr/bin/env bash
# Upload config/recipients.txt to S3. The orchestrator reads the
# recipient list from S3 at run time, so changes go live without
# redeploying the Lambda.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a; source "$ROOT/config.env"; set +a

aws s3 cp "$ROOT/config/recipients.txt" \
  "s3://$S3_BUCKET/config/recipients.txt" --region "$S3_REGION"
