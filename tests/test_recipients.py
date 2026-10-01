"""
Unit tests for get_recipients() in orchestrator/lambda_function.py: the
S3-file-first, env-var-fallback email recipient list.

Run directly: python3 tests/test_recipients.py
"""

import importlib.util
import io
import os
import sys
import unittest

from botocore.exceptions import ClientError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeS3:
    """Minimal S3 double: objects is {key: bytes}. Raises ClientError with
    the given code for a missing key, like real S3 does."""

    def __init__(self, objects=None, error_code="NoSuchKey"):
        self.objects = objects or {}
        self.error_code = error_code

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": self.error_code}}, "GetObject"
            )
        return {"Body": io.BytesIO(self.objects[Key])}


def load_orchestrator(env, s3):
    """Load orchestrator/lambda_function.py fresh, with boto3.client
    stubbed, so each test gets its own module-level state (including the
    recipients cache)."""

    import boto3
    original_client = boto3.client
    original_env = dict(os.environ)

    os.environ.clear()
    os.environ.update({
        "BEDROCK_MODEL_ID": "test-model",
        "S3_BUCKET": "test-bucket",
        "SES_SENDER_EMAIL": "sender@example.com",
        **env,
    })

    clients = {"bedrock-runtime": object(), "s3": s3, "ses": object(),
               "lambda": object()}
    boto3.client = lambda service, **kw: clients[service]

    try:
        spec = importlib.util.spec_from_file_location(
            f"orch_{id(s3)}",
            os.path.join(ROOT, "orchestrator", "lambda_function.py"),
        )
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, os.path.join(ROOT, "orchestrator"))
        spec.loader.exec_module(module)
        return module
    finally:
        boto3.client = original_client
        os.environ.clear()
        os.environ.update(original_env)


class GetRecipientsTests(unittest.TestCase):

    def test_reads_list_from_s3_file(self):
        s3 = FakeS3({
            "config/recipients.txt":
                b"# a comment\na@example.com\nb@example.com\n\n"
        })
        orch = load_orchestrator({"SES_RECIPIENT_EMAIL": ""}, s3)
        self.assertEqual(
            orch.get_recipients(), ["a@example.com", "b@example.com"]
        )

    def test_falls_back_to_env_var_when_file_missing(self):
        s3 = FakeS3({}, error_code="NoSuchKey")
        orch = load_orchestrator(
            {"SES_RECIPIENT_EMAIL": "fallback@example.com"}, s3
        )
        self.assertEqual(orch.get_recipients(), ["fallback@example.com"])

    def test_falls_back_to_env_var_on_access_denied(self):
        # The orchestrator's IAM role might not have s3:GetObject on
        # config/* yet; this must degrade, not crash the whole run.
        s3 = FakeS3({}, error_code="AccessDenied")
        orch = load_orchestrator(
            {"SES_RECIPIENT_EMAIL": "fallback@example.com"}, s3
        )
        self.assertEqual(orch.get_recipients(), ["fallback@example.com"])

    def test_other_client_errors_are_not_swallowed(self):
        s3 = FakeS3({}, error_code="InternalError")
        orch = load_orchestrator(
            {"SES_RECIPIENT_EMAIL": "fallback@example.com"}, s3
        )
        with self.assertRaises(ClientError):
            orch.get_recipients()

    def test_raises_when_nothing_configured(self):
        s3 = FakeS3({}, error_code="NoSuchKey")
        orch = load_orchestrator({"SES_RECIPIENT_EMAIL": ""}, s3)
        with self.assertRaises(RuntimeError):
            orch.get_recipients()

    def test_result_is_cached_after_first_call(self):
        s3 = FakeS3({"config/recipients.txt": b"a@example.com\n"})
        orch = load_orchestrator({"SES_RECIPIENT_EMAIL": ""}, s3)
        first = orch.get_recipients()
        s3.objects["config/recipients.txt"] = b"changed@example.com\n"
        self.assertEqual(orch.get_recipients(), first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
