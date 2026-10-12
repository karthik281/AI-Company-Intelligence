"""
Unit tests for api/lambda_function.py (the profile API) with DynamoDB and
SES faked.

Run directly: python3 tests/test_api_unit.py
"""

import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeDynamo:
    def __init__(self):
        self.items = {}

    def get_item(self, TableName, Key):
        item = self.items.get(Key["email"]["S"])
        return {"Item": item} if item else {}

    def put_item(self, TableName, Item):
        self.items[Item["email"]["S"]] = Item


class FakeSES:
    def __init__(self, fail=False):
        self.verified = []
        self.fail = fail

    def verify_email_identity(self, EmailAddress):
        if self.fail:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "Throttling"}}, "Verify")
        self.verified.append(EmailAddress)


def load_api(dynamo, ses_client):
    import boto3
    original = boto3.client
    original_env = dict(os.environ)
    os.environ["USERS_TABLE"] = "users"
    boto3.client = lambda service, **kw: {
        "dynamodb": dynamo, "ses": ses_client}[service]
    try:
        # taxonomy.py is shared with the orchestrator
        sys.path.insert(0, os.path.join(ROOT, "orchestrator"))
        spec = importlib.util.spec_from_file_location(
            f"api_{id(dynamo)}", os.path.join(ROOT, "api", "lambda_function.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        boto3.client = original
        os.environ.clear()
        os.environ.update(original_env)


def request(method, path, body=None, email="u@x.com", verified="true"):
    claims = {}
    if email:
        claims = {"email": email, "email_verified": verified}
    return {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": claims}},
        },
        "body": json.dumps(body) if body is not None else None,
    }


GOOD = {"roles": ["investor"], "regions": ["EMEA", "APAC"],
        "industries": ["Fintech", "Deep Tech"]}


class ProfileApiTests(unittest.TestCase):

    def setUp(self):
        self.db = FakeDynamo()
        self.ses = FakeSES()
        self.api = load_api(self.db, self.ses)

    def call(self, *a, **kw):
        result = self.api.lambda_handler(request(*a, **kw), None)
        return result["statusCode"], json.loads(result["body"])

    def test_options_are_public(self):
        status, body = self.call("GET", "/options", email=None)
        self.assertEqual(status, 200)
        self.assertIn("EMEA", body["regions"])
        self.assertIn("Construction Tech & Proptech", body["industries"])
        self.assertEqual(body["roles"], ["founder", "investor"])

    def test_profile_requires_verified_email(self):
        self.assertEqual(self.call("GET", "/profile", email=None)[0], 401)
        self.assertEqual(
            self.call("GET", "/profile", verified="false")[0], 401)

    def test_first_get_is_empty_defaults(self):
        status, body = self.call("GET", "/profile")
        self.assertEqual(status, 200)
        self.assertFalse(body["exists"])
        self.assertEqual(body["regions"], [])

    def test_save_then_read_back(self):
        status, saved = self.call("PUT", "/profile", GOOD)
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved["exists"])
        self.assertTrue(saved["created"])
        self.assertFalse(self.call("PUT", "/profile", GOOD)[1]["created"])
        status, got = self.call("GET", "/profile")
        self.assertEqual(got["roles"], ["investor"])
        self.assertEqual(got["regions"], ["EMEA", "APAC"])
        self.assertEqual(got["industries"], ["Deep Tech", "Fintech"])

    def test_update_keeps_created_and_changes_choices(self):
        self.call("PUT", "/profile", GOOD)
        created = self.db.items["u@x.com"]["created"]
        self.call("PUT", "/profile", {**GOOD, "regions": ["LATAM"]})
        item = self.db.items["u@x.com"]
        self.assertEqual(item["created"], created)
        self.assertEqual(item["regions"]["L"], [{"S": "LATAM"}])

    def test_email_comes_from_token_not_body(self):
        self.call("PUT", "/profile", {**GOOD, "email": "victim@x.com"})
        self.assertEqual(list(self.db.items), ["u@x.com"])

    def test_users_cannot_read_each_others_profiles(self):
        self.call("PUT", "/profile", GOOD, email="a@x.com")
        status, body = self.call("GET", "/profile", email="b@x.com")
        self.assertFalse(body["exists"])

    def test_invalid_input_rejected_and_not_saved(self):
        status, body = self.call(
            "PUT", "/profile",
            {"roles": [], "regions": ["Mars"], "industries": ["Fintech"]})
        self.assertEqual(status, 400)
        self.assertEqual(len(body["problems"]), 2)
        self.assertEqual(self.db.items, {})

    def test_bad_json_body(self):
        event = request("PUT", "/profile")
        event["body"] = "not json"
        self.assertEqual(self.api.lambda_handler(event, None)["statusCode"], 400)

    def test_ses_verification_only_on_first_save(self):
        self.call("PUT", "/profile", GOOD)
        self.call("PUT", "/profile", GOOD)
        self.assertEqual(self.ses.verified, ["u@x.com"])

    def test_ses_failure_does_not_lose_profile(self):
        api = load_api(self.db, FakeSES(fail=True))
        result = api.lambda_handler(request("PUT", "/profile", GOOD), None)
        self.assertEqual(result["statusCode"], 200)
        self.assertIn("u@x.com", self.db.items)

    def test_can_pause_emails(self):
        self.call("PUT", "/profile", {**GOOD, "active": False})
        status, body = self.call("GET", "/profile")
        self.assertFalse(body["active"])

    def test_stored_profile_is_readable_by_orchestrator(self):
        """The orchestrator deserialises the same DynamoDB item shape."""
        from boto3.dynamodb.types import TypeDeserializer
        self.call("PUT", "/profile", GOOD)
        raw = {k: TypeDeserializer().deserialize(v)
               for k, v in self.db.items["u@x.com"].items()}
        sys.path.insert(0, os.path.join(ROOT, "orchestrator"))
        import taxonomy
        p = taxonomy.normalise_profile(raw)
        self.assertEqual(p["email"], "u@x.com")
        self.assertEqual(p["roles"], ["investor"])
        self.assertTrue(p["active"])


if __name__ == "__main__":
    unittest.main()
