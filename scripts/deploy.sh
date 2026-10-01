#!/usr/bin/env bash
# Deploy one or both Lambdas.
#
#   scripts/deploy.sh               # both
#   scripts/deploy.sh orchestrator  # Mumbai only
#   scripts/deploy.sh research      # Oregon only

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-all}"

if [ ! -f "$ROOT/config.env" ]; then
  echo "Missing config.env. Copy config.example.env to config.env and fill it in."
  exit 1
fi

set -a; source "$ROOT/config.env"; set +a

"$ROOT/scripts/build.sh" "${ORCH_ARCH:-x86_64}"

deploy() {
  local fn="$1" region="$2" zip="$3"
  # On Windows, the AWS CLI is a native exe and doesn't understand MSYS
  # paths (/c/...), so convert to a Windows path when cygpath exists.
  if command -v cygpath >/dev/null 2>&1; then
    zip="$(cygpath -w "$zip")"
  fi
  echo "Deploying $fn ($region)..."
  aws lambda update-function-code \
    --function-name "$fn" \
    --region "$region" \
    --zip-file "fileb://$zip" \
    --output text --query 'LastModified'
  aws lambda wait function-updated --function-name "$fn" --region "$region"
  echo "Done: $fn"
}

if [ "$TARGET" = "all" ] || [ "$TARGET" = "orchestrator" ]; then
  deploy "$ORCH_FUNCTION" "$ORCH_REGION" "$ROOT/dist/orchestrator.zip"
fi

if [ "$TARGET" = "all" ] || [ "$TARGET" = "research" ]; then
  deploy "$RESEARCH_FUNCTION" "$RESEARCH_REGION" "$ROOT/dist/research.zip"
fi
