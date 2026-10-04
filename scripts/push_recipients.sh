#!/usr/bin/env bash
# Upload config/recipients.txt to S3. The orchestrator reads the
# recipient list from S3 at run time, so changes go live without
# redeploying the Lambda.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a; source "$ROOT/config.env"; set +a

aws s3 cp "$ROOT/config/recipients.txt" \
  "s3://$S3_BUCKET/config/recipients.txt" --region "$S3_REGION"

# SES is sandboxed: every recipient must be a verified identity, and one
# unverified address makes the whole send fail. Trigger verification for
# any address that isn't verified yet and report each one's status. The
# recipient still has to click the link AWS emails them.
echo
echo "SES verification status ($ORCH_REGION):"
pending=0
while IFS= read -r line || [[ -n "$line" ]]; do
  addr="$(echo "$line" | tr -d '\r' | xargs)"
  [[ -z "$addr" || "$addr" == \#* ]] && continue

  status="$(aws ses get-identity-verification-attributes \
    --identities "$addr" --region "$ORCH_REGION" \
    --query "VerificationAttributes.\"$addr\".VerificationStatus" \
    --output text 2>/dev/null || true)"

  if [[ "$status" == "None" || -z "$status" ]]; then
    aws ses verify-email-identity --email-address "$addr" \
      --region "$ORCH_REGION"
    status="Pending (verification email just sent)"
  fi

  [[ "$status" == "Success" ]] && status="Verified" || pending=1
  echo "  $addr: $status"
done < "$ROOT/config/recipients.txt"

if [[ $pending -eq 1 ]]; then
  echo "WARNING: unverified recipients above will make SES reject the" \
       "whole send until they click the verification link."
fi
