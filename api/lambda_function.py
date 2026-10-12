"""
company-intelligence profile API

Sits behind an API Gateway HTTP API with a Cognito JWT authorizer. The
signed-in user's email comes from the verified token claims, never from the
request body, so a user can only read and change their own profile.

Routes
  GET  /options   roles, regions and industries a user can choose from
  GET  /profile   the caller's saved profile (or empty defaults)
  PUT  /profile   save roles / regions / industries (and optional active)

Profiles live in the DynamoDB table USERS_TABLE (partition key `email`),
which the orchestrator reads to decide who gets which report.
"""

import base64
import json
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

import taxonomy

USERS_TABLE = os.environ["USERS_TABLE"]

# While SES is in sandbox, only verified addresses can receive mail. Ask SES
# to verify a user's address the first time they save a profile (SES emails
# them a confirmation link). Turn off once SES has production access.
VERIFY_IN_SES = os.environ.get("VERIFY_IN_SES", "true").lower() == "true"

dynamodb = boto3.client("dynamodb")
ses = boto3.client("ses", region_name=os.environ.get("SES_REGION", "ap-south-1"))

_serialise = TypeSerializer().serialize
_deserialise = TypeDeserializer().deserialize


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body),
    }


def options_payload():
    return {
        "roles": taxonomy.ALL_ROLES,
        "regions": taxonomy.ALL_REGIONS,
        "industries": taxonomy.ALL_INDUSTRIES,
    }


def caller_email(event):
    """Verified email from the JWT claims, or None."""

    claims = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    ) or {}

    email = str(claims.get("email", "")).strip().lower()
    verified = str(claims.get("email_verified", "")).lower() == "true"

    if not email or "@" not in email or not verified:
        return None

    return email


def load_profile(email):

    response = dynamodb.get_item(
        TableName=USERS_TABLE,
        Key={"email": {"S": email}},
    )

    item = response.get("Item")

    if not item:
        return None

    return {k: _deserialise(v) for k, v in item.items()}


def public_profile(item):
    """What the UI sees: the stored choices, not internal fields."""

    return {
        "email": item["email"],
        "roles": item.get("roles", []),
        "regions": item.get("regions", []),
        "industries": item.get("industries", []),
        "active": item.get("active", True),
        "updated": item.get("updated"),
    }


def parse_body(event):

    body = event.get("body") or ""

    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    try:
        parsed = json.loads(body) if body else {}
    except ValueError:
        return None

    return parsed if isinstance(parsed, dict) else None


def save_profile(email, submitted):

    existing = load_profile(email)
    now = datetime.now(timezone.utc).isoformat()

    # Store the canonical spellings, in the standard order.
    clean = taxonomy.normalise_profile({**submitted, "email": email})

    item = {
        "email": email,
        "roles": clean["roles"],
        "regions": clean["regions"],
        "industries": clean["industries"],
        "active": submitted.get("active", True) is not False,
        "created": (existing or {}).get("created", now),
        "updated": now,
    }

    dynamodb.put_item(
        TableName=USERS_TABLE,
        Item={k: _serialise(v) for k, v in item.items()},
    )

    return item, existing is None


def request_ses_verification(email):

    try:
        ses.verify_email_identity(EmailAddress=email)
    except ClientError as e:
        # A failure here must not lose the saved profile.
        print(f"WARNING: could not start SES verification for {email}: {e}")


def lambda_handler(event, context):

    http = (event.get("requestContext") or {}).get("http", {})
    method = http.get("method") or event.get("httpMethod", "")
    path = (event.get("rawPath") or event.get("path") or "").rstrip("/")

    if path.endswith("/options") and method == "GET":
        return respond(200, options_payload())

    if not path.endswith("/profile"):
        return respond(404, {"error": "Not found"})

    email = caller_email(event)

    if not email:
        return respond(401, {"error": "A verified email is required"})

    if method == "GET":

        item = load_profile(email)

        if item is None:
            return respond(200, {
                "email": email,
                "exists": False,
                "roles": [],
                "regions": [],
                "industries": [],
                "active": True,
            })

        return respond(200, {**public_profile(item), "exists": True})

    if method == "PUT":

        submitted = parse_body(event)

        if submitted is None:
            return respond(400, {"error": "Body must be a JSON object"})

        problems = taxonomy.validate_profile_input(submitted)

        if problems:
            return respond(400, {"error": "Invalid profile", "problems": problems})

        item, created = save_profile(email, submitted)

        if created and VERIFY_IN_SES:
            request_ses_verification(email)

        return respond(200, {
            **public_profile(item),
            "exists": True,
            "created": created,
        })

    return respond(405, {"error": "Method not allowed"})
