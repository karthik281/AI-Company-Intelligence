#!/usr/bin/env bash
# One-time (idempotent) setup of the Dawnbrief multi-user layer in AWS.
#
# Creates, in ORCH_REGION, only what is missing:
#   - DynamoDB table        dawnbrief-users        (user profiles)
#   - IAM role + Lambda     dawnbrief-profile-api  (profile API)
#   - Cognito user pool, hosted-login domain and a public app client
#   - HTTP API with a Cognito JWT authorizer (GET /options is public)
#   - private S3 bucket + CloudFront distribution (free https URL) for site/
# and then grants the orchestrator role what the per-user pipeline needs
# (read profiles, read/write per-user history, send to any verified identity).
#
# Re-running is safe. Use --no-orchestrator to skip the orchestrator
# permission/env changes, or --site-only to only republish site/.
#
#   scripts/setup_v2.sh [--no-orchestrator] [--site-only]

set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a; source "$ROOT/config.env"; set +a

REGION="$ORCH_REGION"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
NAME="dawnbrief"
TABLE="$NAME-users"
API_FN="${API_FUNCTION:-$NAME-profile-api}"
API_ROLE="DawnbriefProfileApiRole"
POOL_NAME="$NAME-users"
SITE_BUCKET="$NAME-site-$ACCOUNT"
ORCH_ROLE="${ORCH_ROLE:-CompanyIntelligenceLambdaRole}"
SES_REGION="$REGION"

DO_ORCH=1; SITE_ONLY=0
for a in "$@"; do
  case "$a" in
    --no-orchestrator) DO_ORCH=0 ;;
    --site-only) SITE_ONLY=1 ;;
  esac
done

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# The AWS CLI on Windows needs native paths for file:// arguments.
wp() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }
say() { echo "==> $*"; }

# ---------------------------------------------------------------- site (S3 + CloudFront)
ensure_site_bucket() {
  if ! aws s3api head-bucket --bucket "$SITE_BUCKET" --region "$REGION" 2>/dev/null; then
    say "Creating private site bucket $SITE_BUCKET"
    aws s3api create-bucket --bucket "$SITE_BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$SITE_BUCKET" --region "$REGION" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
}

ensure_distribution() {
  DIST_ID="$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Comment=='Dawnbrief site'].Id | [0]" --output text 2>/dev/null || true)"
  if [ -z "$DIST_ID" ] || [ "$DIST_ID" = "None" ]; then
    say "Creating CloudFront origin access control and distribution"
    OAC_ID="$(aws cloudfront list-origin-access-controls \
      --query "OriginAccessControlList.Items[?Name=='$NAME-oac'].Id | [0]" --output text 2>/dev/null || true)"
    if [ -z "$OAC_ID" ] || [ "$OAC_ID" = "None" ]; then
      OAC_ID="$(aws cloudfront create-origin-access-control --origin-access-control-config \
        "Name=$NAME-oac,Description=Dawnbrief site,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=s3" \
        --query OriginAccessControl.Id --output text)"
    fi
    cat > "$TMP/dist.json" <<EOF
{
  "CallerReference": "$NAME-$(date +%s)",
  "Comment": "Dawnbrief site",
  "Enabled": true,
  "DefaultRootObject": "index.html",
  "PriceClass": "PriceClass_100",
  "HttpVersion": "http2and3",
  "Origins": {"Quantity": 1, "Items": [{
    "Id": "site",
    "DomainName": "$SITE_BUCKET.s3.$REGION.amazonaws.com",
    "OriginAccessControlId": "$OAC_ID",
    "S3OriginConfig": {"OriginAccessIdentity": ""}
  }]},
  "DefaultCacheBehavior": {
    "TargetOriginId": "site",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
    "Compress": true,
    "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
    "ResponseHeadersPolicyId": "67f7725c-6f97-4210-82d7-5512b698c20c"
  }
}
EOF
    DIST_ID="$(aws cloudfront create-distribution \
      --distribution-config "file://$(wp "$TMP/dist.json")" \
      --query Distribution.Id --output text)"
  fi
  DIST_DOMAIN="$(aws cloudfront get-distribution --id "$DIST_ID" \
    --query Distribution.DomainName --output text)"
  SITE_URL="https://$DIST_DOMAIN"

  cat > "$TMP/bucket-policy.json" <<EOF
{"Version":"2012-10-17","Statement":[{
  "Sid":"AllowCloudFrontRead","Effect":"Allow",
  "Principal":{"Service":"cloudfront.amazonaws.com"},
  "Action":"s3:GetObject","Resource":"arn:aws:s3:::$SITE_BUCKET/*",
  "Condition":{"StringEquals":{"AWS:SourceArn":"arn:aws:cloudfront::$ACCOUNT:distribution/$DIST_ID"}}
}]}
EOF
  aws s3api put-bucket-policy --bucket "$SITE_BUCKET" --region "$REGION" \
    --policy "file://$(wp "$TMP/bucket-policy.json")"
}

publish_site() {
  say "Publishing site/ to $SITE_URL"
  aws s3 sync "$ROOT/site/" "s3://$SITE_BUCKET/" --region "$REGION" --delete \
    --exclude "config.js" --cache-control "public, max-age=300"
  aws s3 cp "$TMP/config.js" "s3://$SITE_BUCKET/config.js" --region "$REGION" \
    --content-type "application/javascript" --cache-control "no-cache"
  aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" >/dev/null
}

# ---------------------------------------------------------------- data + API
ensure_table() {
  if ! aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1; then
    say "Creating DynamoDB table $TABLE"
    aws dynamodb create-table --table-name "$TABLE" --region "$REGION" \
      --attribute-definitions AttributeName=email,AttributeType=S \
      --key-schema AttributeName=email,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST >/dev/null
    aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
    aws dynamodb update-continuous-backups --table-name "$TABLE" --region "$REGION" \
      --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true >/dev/null
  fi
  TABLE_ARN="arn:aws:dynamodb:$REGION:$ACCOUNT:table/$TABLE"
}

ensure_api_role() {
  if ! aws iam get-role --role-name "$API_ROLE" >/dev/null 2>&1; then
    say "Creating IAM role $API_ROLE"
    cat > "$TMP/trust.json" <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
    aws iam create-role --role-name "$API_ROLE" \
      --assume-role-policy-document "file://$(wp "$TMP/trust.json")" >/dev/null
    aws iam attach-role-policy --role-name "$API_ROLE" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    NEW_ROLE=1
  fi
  cat > "$TMP/api-policy.json" <<EOF
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["dynamodb:GetItem","dynamodb:PutItem"],"Resource":"$TABLE_ARN"},
  {"Effect":"Allow","Action":"ses:VerifyEmailIdentity","Resource":"*"}
]}
EOF
  aws iam put-role-policy --role-name "$API_ROLE" --policy-name DawnbriefProfileApiAccess \
    --policy-document "file://$(wp "$TMP/api-policy.json")"
  API_ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$API_ROLE"
  # IAM is eventually consistent; a brand-new role needs a moment before Lambda can use it.
  if [ "${NEW_ROLE:-0}" = "1" ]; then sleep 12; fi
}

ensure_api_lambda() {
  if [ ! -f "$ROOT/dist/api.zip" ]; then "$ROOT/scripts/build.sh" "${ORCH_ARCH:-x86_64}"; fi
  if ! aws lambda get-function --function-name "$API_FN" --region "$REGION" >/dev/null 2>&1; then
    say "Creating Lambda $API_FN"
    aws lambda create-function --function-name "$API_FN" --region "$REGION" \
      --runtime python3.14 --handler lambda_function.lambda_handler \
      --role "$API_ROLE_ARN" --timeout 15 --memory-size 256 \
      --environment "Variables={USERS_TABLE=$TABLE}" \
      --zip-file "fileb://$(wp "$ROOT/dist/api.zip")" >/dev/null
    aws lambda wait function-active-v2 --function-name "$API_FN" --region "$REGION"
  fi
  API_FN_ARN="$(aws lambda get-function --function-name "$API_FN" --region "$REGION" \
    --query Configuration.FunctionArn --output text)"
}

# ---------------------------------------------------------------- sign-in
ensure_cognito() {
  POOL_ID="$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
    --query "UserPools[?Name=='$POOL_NAME'].Id | [0]" --output text)"
  if [ -z "$POOL_ID" ] || [ "$POOL_ID" = "None" ]; then
    say "Creating Cognito user pool $POOL_NAME"
    POOL_ID="$(aws cognito-idp create-user-pool --pool-name "$POOL_NAME" --region "$REGION" \
      --alias-attributes email preferred_username \
      --auto-verified-attributes email \
      --username-configuration CaseSensitive=false \
      --policies "PasswordPolicy={MinimumLength=10,RequireUppercase=true,RequireLowercase=true,RequireNumbers=true,RequireSymbols=false}" \
      --account-recovery-setting "RecoveryMechanisms=[{Priority=1,Name=verified_email}]" \
      --query UserPool.Id --output text)"
  fi

  # Hosted-login domain: the prefix becomes part of the sign-in URL.
  DOMAIN_PREFIX="$(aws cognito-idp describe-user-pool --user-pool-id "$POOL_ID" --region "$REGION" \
    --query UserPool.Domain --output text)"
  if [ -z "$DOMAIN_PREFIX" ] || [ "$DOMAIN_PREFIX" = "None" ]; then
    for candidate in "$NAME" "$NAME-app" "$NAME-$(echo "$ACCOUNT" | tail -c 7)"; do
      if aws cognito-idp create-user-pool-domain --domain "$candidate" \
           --user-pool-id "$POOL_ID" --region "$REGION" >/dev/null 2>&1; then
        DOMAIN_PREFIX="$candidate"; break
      fi
    done
  fi
  [ -n "$DOMAIN_PREFIX" ] && [ "$DOMAIN_PREFIX" != "None" ] || { echo "Could not create a Cognito domain"; exit 1; }
  COGNITO_DOMAIN="$DOMAIN_PREFIX.auth.$REGION.amazoncognito.com"

  CLIENT_ID="$(aws cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --region "$REGION" \
    --query "UserPoolClients[?ClientName=='$NAME-web'].ClientId | [0]" --output text)"
  CLIENT_ARGS=(--user-pool-id "$POOL_ID" --region "$REGION" --client-name "$NAME-web"
    --no-generate-secret
    --allowed-o-auth-flows code --allowed-o-auth-flows-user-pool-client
    --allowed-o-auth-scopes openid email profile
    --supported-identity-providers COGNITO
    --callback-urls "$SITE_URL/" "http://localhost:8080/"
    --logout-urls "$SITE_URL/" "http://localhost:8080/"
    --explicit-auth-flows ALLOW_REFRESH_TOKEN_AUTH
    --prevent-user-existence-errors ENABLED
    --id-token-validity 60 --access-token-validity 60 --refresh-token-validity 30
    --token-validity-units "AccessToken=minutes,IdToken=minutes,RefreshToken=days")
  if [ -z "$CLIENT_ID" ] || [ "$CLIENT_ID" = "None" ]; then
    say "Creating Cognito app client"
    CLIENT_ID="$(aws cognito-idp create-user-pool-client "${CLIENT_ARGS[@]}" \
      --query UserPoolClient.ClientId --output text)"
  else
    aws cognito-idp update-user-pool-client "${CLIENT_ARGS[@]}" --client-id "$CLIENT_ID" >/dev/null
  fi
}

ensure_http_api() {
  API_ID="$(aws apigatewayv2 get-apis --region "$REGION" \
    --query "Items[?Name=='$NAME-api'].ApiId | [0]" --output text)"
  if [ -z "$API_ID" ] || [ "$API_ID" = "None" ]; then
    say "Creating HTTP API"
    API_ID="$(aws apigatewayv2 create-api --name "$NAME-api" --protocol-type HTTP \
      --region "$REGION" --query ApiId --output text)"
  fi
  aws apigatewayv2 update-api --api-id "$API_ID" --region "$REGION" \
    --cors-configuration "AllowOrigins=$SITE_URL,http://localhost:8080,AllowMethods=GET,PUT,OPTIONS,AllowHeaders=authorization,content-type,MaxAge=3600" >/dev/null

  INTEGRATION_ID="$(aws apigatewayv2 get-integrations --api-id "$API_ID" --region "$REGION" \
    --query "Items[0].IntegrationId" --output text)"
  if [ -z "$INTEGRATION_ID" ] || [ "$INTEGRATION_ID" = "None" ]; then
    INTEGRATION_ID="$(aws apigatewayv2 create-integration --api-id "$API_ID" --region "$REGION" \
      --integration-type AWS_PROXY --integration-uri "$API_FN_ARN" \
      --payload-format-version 2.0 --query IntegrationId --output text)"
  fi

  AUTH_ID="$(aws apigatewayv2 get-authorizers --api-id "$API_ID" --region "$REGION" \
    --query "Items[?Name=='cognito'].AuthorizerId | [0]" --output text)"
  ISSUER="https://cognito-idp.$REGION.amazonaws.com/$POOL_ID"
  if [ -z "$AUTH_ID" ] || [ "$AUTH_ID" = "None" ]; then
    AUTH_ID="$(aws apigatewayv2 create-authorizer --api-id "$API_ID" --region "$REGION" \
      --name cognito --authorizer-type JWT \
      --identity-source '$request.header.Authorization' \
      --jwt-configuration "Audience=$CLIENT_ID,Issuer=$ISSUER" \
      --query AuthorizerId --output text)"
  else
    aws apigatewayv2 update-authorizer --api-id "$API_ID" --authorizer-id "$AUTH_ID" --region "$REGION" \
      --jwt-configuration "Audience=$CLIENT_ID,Issuer=$ISSUER" >/dev/null
  fi

  existing_routes="$(aws apigatewayv2 get-routes --api-id "$API_ID" --region "$REGION" \
    --query "Items[].RouteKey" --output text)"
  add_route() {  # route-key, auth(JWT|NONE)
    case " $existing_routes " in *"$1"*) return ;; esac
    if [ "$2" = "JWT" ]; then
      aws apigatewayv2 create-route --api-id "$API_ID" --region "$REGION" --route-key "$1" \
        --target "integrations/$INTEGRATION_ID" --authorization-type JWT \
        --authorizer-id "$AUTH_ID" >/dev/null
    else
      aws apigatewayv2 create-route --api-id "$API_ID" --region "$REGION" --route-key "$1" \
        --target "integrations/$INTEGRATION_ID" --authorization-type NONE >/dev/null
    fi
  }
  add_route "GET /options" NONE
  add_route "GET /profile" JWT
  add_route "PUT /profile" JWT

  aws apigatewayv2 get-stage --api-id "$API_ID" --stage-name '$default' --region "$REGION" >/dev/null 2>&1 || \
    aws apigatewayv2 create-stage --api-id "$API_ID" --stage-name '$default' --auto-deploy \
      --default-route-settings "ThrottlingBurstLimit=20,ThrottlingRateLimit=10" --region "$REGION" >/dev/null

  aws lambda add-permission --function-name "$API_FN" --region "$REGION" \
    --statement-id apigw-invoke --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT:$API_ID/*/*" >/dev/null 2>&1 || true

  API_URL="https://$API_ID.execute-api.$REGION.amazonaws.com"
}

# ---------------------------------------------------------------- orchestrator
grant_orchestrator() {
  say "Granting $ORCH_ROLE access to profiles, per-user history and SES identities"
  cat > "$TMP/orch-policy.json" <<EOF
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":"dynamodb:Scan","Resource":"$TABLE_ARN"},
  {"Effect":"Allow","Action":"s3:GetObject","Resource":"arn:aws:s3:::$S3_BUCKET/state/*"}
]}
EOF
  aws iam put-role-policy --role-name "$ORCH_ROLE" --policy-name DawnbriefOrchestratorAccess \
    --policy-document "file://$(wp "$TMP/orch-policy.json")"

  # Every signed-up user is its own SES identity, so sending can't be
  # scoped to a fixed list of addresses any more.
  cat > "$TMP/ses-policy.json" <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Action":["ses:SendEmail","ses:SendRawEmail"],
  "Resource":"arn:aws:ses:$SES_REGION:$ACCOUNT:identity/*"}]}
EOF
  aws iam put-role-policy --role-name "$ORCH_ROLE" --policy-name CompanyIntelligenceSESPolicy \
    --policy-document "file://$(wp "$TMP/ses-policy.json")"

  # Merge USERS_TABLE into the existing environment (update replaces it).
  aws lambda get-function-configuration --function-name "$ORCH_FUNCTION" --region "$REGION" \
    --query Environment.Variables --output json > "$TMP/env-old.json"
  python3 - "$TMP/env-old.json" "$TMP/env-new.json" "$TABLE" <<'PY'
import json, sys
env = json.load(open(sys.argv[1])) or {}
env["USERS_TABLE"] = sys.argv[3]
json.dump({"Variables": env}, open(sys.argv[2], "w"))
PY
  aws lambda update-function-configuration --function-name "$ORCH_FUNCTION" --region "$REGION" \
    --environment "file://$(wp "$TMP/env-new.json")" >/dev/null
  aws lambda wait function-updated --function-name "$ORCH_FUNCTION" --region "$REGION"
}

# ---------------------------------------------------------------- run
ensure_site_bucket
ensure_distribution

if [ "$SITE_ONLY" = "1" ]; then
  # Reuse the live settings already baked into the published config.js.
  aws s3 cp "s3://$SITE_BUCKET/config.js" "$TMP/config.js" --region "$REGION" >/dev/null
  publish_site
  echo "Site: $SITE_URL"; exit 0
fi

ensure_table
ensure_api_role
ensure_api_lambda
ensure_cognito
ensure_http_api

cat > "$TMP/config.js" <<EOF
window.DAWNBRIEF = {
  apiUrl: "$API_URL",
  cognitoDomain: "$COGNITO_DOMAIN",
  clientId: "$CLIENT_ID"
};
EOF
publish_site

if [ "$DO_ORCH" = "1" ]; then grant_orchestrator; fi

echo
echo "Dawnbrief is set up."
echo "  Site:            $SITE_URL"
echo "  Sign-in domain:  https://$COGNITO_DOMAIN"
echo "  API:             $API_URL"
echo "  Users table:     $TABLE"
