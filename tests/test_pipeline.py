"""End-to-end test of both Lambdas with AWS mocked out."""

import email
import importlib.util
import io
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "out")
os.makedirs(OUT, exist_ok=True)


# ------------------------------------------------------------------
# Fake AWS
# ------------------------------------------------------------------

class FakeBedrock:
    """Returns canned responses in order of calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeS3:

    class exceptions:
        pass

    def __init__(self):
        self.objects = {
            "prompts/funding_classifier.md": b"Classify funding events.",
            "prompts/company_report.md": b"Write a company report.",
            "config/recipients.txt": b"# recipients\nrao.kar@gmail.com\nsecond@example.com\n",
        }

    def get_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def head_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}


class FakeSES:
    def __init__(self):
        self.sent = []

    def send_raw_email(self, **kwargs):
        self.sent.append(kwargs)
        return {"MessageId": "test-message-id"}


class FakeLambda:
    def __init__(self, handler):
        self.handler = handler

    def invoke(self, FunctionName, InvocationType, Payload):
        result = self.handler(json.loads(Payload), None)
        return {"Payload": io.BytesIO(json.dumps(result).encode())}


def load(name, path, clients):
    import boto3
    original = boto3.client
    boto3.client = lambda service, **kw: clients[service]
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, os.path.dirname(path))
        spec.loader.exec_module(module)
        return module
    finally:
        boto3.client = original


def text_response(text, stop="end_turn"):
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": stop,
    }


# ------------------------------------------------------------------
# Canned model output
# ------------------------------------------------------------------

CEOVINE = "https://ceovine.in/unveilr-ai-raises-16-7-crore-pre-seed-funding-from-ajvc/"
MINT = "https://www.livemint.com/companies/start-ups/unveilr-ai-raises-pre-seed-from-ajvc-at-rs-16-7-cr-valuation-11719561.html?utm_source=x"
SITE = "https://www.unveilr.com/"

notes_text = (
    "## Executive facts\n"
    "- Unveilr AI builds AI search visibility agents. (CONFIRMED, HIGH)"
)
grounded_response = {
    "stopReason": "end_turn",
    "usage": {"inputTokens": 100, "outputTokens": 900},
    "output": {"message": {"content": [
        {"text": notes_text},
        {"citationsContent": {"citations": [
            {"location": {"web": {"url": SITE, "domain": "unveilr.com"}}}
        ]}},
        # text and citation in the same block (other documented shape)
        {"text": "\n## Funding\n- Pre-seed led by AJVC at a Rs 16.7 crore "
                 "valuation; amount raised not disclosed. (CONFIRMED, HIGH)",
         "citationsContent": {"citations": [
             {"location": {"web": {"url": CEOVINE, "domain": "ceovine.in"}}},
             {"location": {"web": {"url": MINT, "domain": "livemint.com"}}},
         ]}},
        {"text": "\n## Competitors\n- Profound: direct competitor. "
                 "(CONFIRMED, MEDIUM)" + " filler" * 120},
        {"citationsContent": {"citations": [
            # duplicate of MINT without tracking param
            {"location": {"web": {"url": MINT.split("?")[0], "domain": "livemint.com"}}}
        ]}},
    ]}},
}

packet_json = {
    "company": "Unveilr AI",
    "executive_facts": [
        {"claim": "Unveilr AI builds AI search visibility agents",
         "status": "CONFIRMED", "confidence": "HIGH",
         "evidence": "Company website", "source_ids": ["S1"]}
    ],
    "claims": [
        {"claim": "Founded in 2025", "status": "CONFIRMED",
         "confidence": "MEDIUM", "evidence": "", "source_ids": []},
        {"claim": "Uses managed-service model", "status": "CONFIRMED",
         "confidence": "HIGH", "evidence": "Mint article",
         "source_ids": ["S3", "S9"]},
    ],
    "customers": [
        {"organization": "No named customers found", "classification": "unknown",
         "evidence": "Company website lists no client logos", "source_ids": ["S1"]}
    ],
    "technology": [
        {"area": "AI / ML", "claim": "Uses AI agents to analyse AI-search answers",
         "status": "CONFIRMED", "confidence": "MEDIUM",
         "evidence": "Product description on company website", "source_ids": ["S1"]},
        {"area": "Agentic AI", "claim": "Autonomous multi-step execution",
         "status": "UNKNOWN", "confidence": "LOW",
         "evidence": "Marketing uses the word agents; no technical detail", "source_ids": []},
        {"area": "Infrastructure", "claim": "Cloud provider not publicly established",
         "status": "UNKNOWN", "confidence": "LOW", "evidence": "", "source_ids": []}
    ],
    "competitors": [
        {"name": "Profound", "category": "direct",
         "why_relevant": "AI visibility analytics; raised a $96M Series C at a $1B valuation",
         "difference": "Software platform rather than managed service",
         "source_ids": ["S3"]},
        {"name": "Peec AI", "category": "direct",
         "why_relevant": "AI search tracking; about $30M raised across seed and Series A",
         "difference": "Self-serve product", "source_ids": ["S3"]},
        {"name": "In-house SEO teams", "category": "internal_build",
         "why_relevant": "Brands can monitor AI answers themselves",
         "difference": "Needs tooling and expertise", "source_ids": []}
    ],
    "funding": [
        {"date": "2026-10-01", "round": "pre-seed",
         "amount": "Not publicly disclosed", "lead_investor": "AJVC",
         "other_investors": [], "valuation": "₹16.7 crore",
         "source_ids": ["S2"]}
    ],
    "unknowns": [
        {"question": "What is current revenue?", "reason": "No revenue figures in any retrieved source"},
        {"question": "Who are the founders?", "reason": "Founders are not named in funding coverage"}
    ],
}

# First formatter answer is broken JSON (tests the formatter-only retry).
formatter_bad = text_response('{"company": "Unveilr AI", "claims": [{"claim": "unterminated')
formatter_good = text_response("```json\n" + json.dumps(packet_json, ensure_ascii=False) + "\n```")

classifier_out = text_response(json.dumps({"events": [{
    "relevant": True, "company": "Unveilr AI", "event_type": "funding",
    "round": "Pre-Seed", "amount": "Rs 16.7 Cr", "investors": [],
    "lead_investor": "Not publicly disclosed",
    "event_date": "Not publicly disclosed",
    "source_title": "Unveilr AI Raises Pre-Seed Funding at Rs 16.7 Cr Valuation to Build AI Search Agents",
    "source_url": "https://news.google.com/rss/articles/abc",
}]}))

report_md = open(os.path.join(ROOT, "tests", "detailed_report.md")).read()
report_out = text_response(report_md)


# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------

os.environ.update({
    "BEDROCK_MODEL_ID": "test-model",
    "S3_BUCKET": "company-intelligence-2026",
    "SES_SENDER_EMAIL": "rao.kar@gmail.com",
    "SES_RECIPIENT_EMAIL": "rao.kar@gmail.com",
    "SAVE_PDF_TO_S3": "true",
    "DEDUPE_EVENTS": "true",
})

research_bedrock = FakeBedrock([grounded_response, formatter_bad, formatter_good])
research = load(
    "research_lambda",
    os.path.join(ROOT, "research", "lambda_function.py"),
    {"bedrock-runtime": research_bedrock},
)

orch_bedrock = FakeBedrock([classifier_out, report_out])
s3 = FakeS3()
ses = FakeSES()
orch = load(
    "orch_lambda",
    os.path.join(ROOT, "orchestrator", "lambda_function.py"),
    {
        "bedrock-runtime": orch_bedrock,
        "s3": s3,
        "ses": ses,
        "lambda": FakeLambda(research.lambda_handler),
    },
)

news = [{"title": "Unveilr AI Raises Pre-Seed Funding at Rs 16.7 Cr Valuation to Build AI Search Agents - Mint",
         "url": "https://news.google.com/rss/articles/abc", "published": "x",
         "description": "d", "publisher": "Mint", "publisher_url": "https://www.livemint.com"},
        {"title": "OtherCo raises $50M Series B", "url": "https://news.google.com/rss/articles/other",
         "published": "x", "description": "d", "publisher": "TC", "publisher_url": "https://techcrunch.com"}]
orch.fetch_rss = lambda q, limit: news if "funding" in q else news[:1]

result = orch.lambda_handler({}, None)
body = json.loads(result["body"])
print("STATUS", result["statusCode"])
print(json.dumps(body, indent=2, ensure_ascii=False)[:3000])

# --- checks -------------------------------------------------------
assert result["statusCode"] == 200, result
r = body["reports"][0]
assert r["email_attachment"] == "pdf"
assert r["web_research_sources"] == 3, r["web_research_sources"]
assert r["unverified_citations"] == 0, r
assert r["event"]["amount"] == "Not publicly disclosed", r["event"]
assert r["event"]["valuation"] == "₹16.7 crore", r["event"]
assert len(r["warnings"]) == len(set(r["warnings"])), r["warnings"]
assert r["event"]["lead_investor"] == "AJVC", r["event"]
assert r["event"]["event_date"] == "2026-10-01", r["event"]
assert r["s3_key"].startswith("reports/") and r["s3_key"].endswith("/Unveilr_AI.md")
assert any(k.startswith("state/reported/") for k in s3.objects)

# research: only real URLs, invalid ID dropped, unsourced CONFIRMED downgraded

# report prompt must not include OtherCo
report_prompt = orch_bedrock.calls[1]["messages"][0]["content"][0]["text"]
assert "OtherCo" not in report_prompt, "unrelated article leaked into prompt"
assert "SOURCE REGISTER" in report_prompt

# research formatter was retried without a second grounding call
assert len(research_bedrock.calls) == 3
assert "toolConfig" in research_bedrock.calls[0]
assert "toolConfig" not in research_bedrock.calls[1]
grounded_prompt = research_bedrock.calls[0]["messages"][0]["content"][0]["text"]
assert "Do NOT write URLs" in grounded_prompt
fmt_prompt = research_bedrock.calls[1]["messages"][0]["content"][0]["text"]
assert "[S1]" in fmt_prompt and "[S2, S3]" in fmt_prompt, fmt_prompt[:2000]

# email
assert ses.sent[0]["Destinations"] == ["rao.kar@gmail.com", "second@example.com"], \
    ses.sent[0]["Destinations"]
raw = ses.sent[0]["RawMessage"]["Data"]
msg = email.message_from_bytes(raw)
assert msg["To"] == "rao.kar@gmail.com, second@example.com", msg["To"]
assert msg["Date"] and msg["Message-ID"]
parts = [p.get_content_type() for p in msg.walk()]
print("MIME parts:", parts)
assert "application/pdf" in parts and "text/html" in parts
pdf = next(p for p in msg.walk() if p.get_content_type() == "application/pdf")
print("Attachment:", pdf.get_filename())
open(os.path.join(OUT, "email_attachment.pdf"), "wb").write(pdf.get_payload(decode=True))
open(os.path.join(OUT, "email.eml"), "wb").write(raw)
html = next(p for p in msg.walk() if p.get_content_type() == "text/html")
open(os.path.join(OUT, "email_body.html"), "wb").write(html.get_payload(decode=True))

# second run: deduped
orch_bedrock.responses = [classifier_out]
res2 = orch.lambda_handler({}, None)
b2 = json.loads(res2["body"])
assert b2["skipped_already_reported"] == ["Unveilr AI"], b2
print("Dedupe OK")

print("ALL CHECKS PASSED")
