"""
Handler-level tests for the multi-user flow: users -> news -> events ->
one event per user -> one report per distinct event -> per-user history.
News, classification and report generation are stubbed; the real matching,
grouping, sent-log and response code is exercised.

Run directly: python3 tests/test_multiuser.py
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "orchestrator"))

from test_recipients import FakeS3, load_orchestrator  # noqa: E402


class RWS3(FakeS3):
    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body


USERS = [
    {"email": "eu-founder@x.com", "roles": ["founder"],
     "regions": ["EMEA"], "industries": ["Fintech"]},
    {"email": "apac-founder@x.com", "roles": ["founder"],
     "regions": ["APAC"], "industries": ["AI/ML"]},
    {"email": "investor@x.com", "roles": ["investor"],
     "regions": ["North America"], "industries": ["Deep Tech"]},
]

EVENTS = [
    {"company": "Berlin Pay", "kind": "startup_round", "region": "EMEA",
     "industry": "Fintech", "round": "Seed"},
    {"company": "Pune AI", "kind": "startup_round", "region": "APAC",
     "industry": "AI/ML", "round": "Series A"},
    {"company": "Atlas Capital", "kind": "investor_activity",
     "region": "North America", "industry": "Deep Tech", "round": "Fund II"},
    {"company": "Unrelated Co", "kind": "startup_round",
     "region": "LATAM", "industry": "Edtech", "round": "Seed"},
]


def build(s3=None):
    s3 = s3 or RWS3({"config/users.json": json.dumps(USERS).encode()})
    mod = load_orchestrator({}, s3)
    mod.get_news = lambda users=None: [{"title": "x", "url": "u"}]
    mod.classify_articles = lambda articles: {
        "events": [dict(e) for e in EVENTS]}
    processed = []

    def fake_process(funding_event, articles, run_time, recipients=None):
        processed.append((funding_event["company"], list(recipients)))
        return {
            "company": funding_event["company"],
            "s3_key": f"reports/{funding_event['company']}.md",
            "delivered_to": list(recipients),
        }

    mod.process_event = fake_process
    return mod, s3, processed


def run(mod, event=None):
    result = mod.lambda_handler(event or {}, None)
    return result["statusCode"], json.loads(result["body"])


class MultiUserHandlerTests(unittest.TestCase):

    def test_each_user_gets_one_matching_event(self):
        mod, s3, processed = build()
        status, body = run(mod)
        self.assertEqual(status, 200)
        self.assertEqual(body["assignments"], {
            "eu-founder@x.com": "Berlin Pay",
            "apac-founder@x.com": "Pune AI",
            "investor@x.com": "Atlas Capital",
        })
        self.assertEqual(body["users_without_match"], [])
        # one report per distinct event; the unrelated event is never researched
        self.assertEqual(sorted(c for c, _ in processed),
                         ["Atlas Capital", "Berlin Pay", "Pune AI"])

    def test_no_repeat_and_no_second_event_on_same_day_rerun(self):
        mod, s3, processed = build()
        run(mod)
        processed.clear()
        status, body = run(mod)
        # Everyone already got what matched them; nothing left for them.
        self.assertEqual(processed, [])
        self.assertEqual(len(body["users_without_match"]), 3)

    def test_force_ignores_history(self):
        mod, s3, processed = build()
        run(mod)
        processed.clear()
        run(mod, {"force": True})
        self.assertEqual(len(processed), 3)

    def test_users_sharing_interests_share_one_report(self):
        users = [
            {"email": f"u{i}@x.com", "roles": ["founder"],
             "regions": ["EMEA"], "industries": ["Fintech"]}
            for i in range(4)
        ]
        s3 = RWS3({"config/users.json": json.dumps(users).encode()})
        mod, _, processed = build(s3)
        status, body = run(mod)
        self.assertEqual(len(processed), 1)
        company, recipients = processed[0]
        self.assertEqual(company, "Berlin Pay")
        self.assertEqual(len(recipients), 4)

    def test_failed_delivery_is_retried_next_run(self):
        mod, s3, processed = build()
        original = mod.process_event

        def partial(funding_event, articles, run_time, recipients=None):
            result = original(funding_event, articles, run_time, recipients)
            if funding_event["company"] == "Pune AI":
                result["delivered_to"] = []   # SES rejected it
            return result

        mod.process_event = partial
        run(mod)
        processed.clear()
        mod.process_event = original
        run(mod)
        self.assertEqual([c for c, _ in processed], ["Pune AI"])

    def test_one_failing_event_does_not_stop_the_others(self):
        mod, s3, processed = build()
        original = mod.process_event

        def flaky(funding_event, articles, run_time, recipients=None):
            if funding_event["company"] == "Berlin Pay":
                raise RuntimeError("research failed")
            return original(funding_event, articles, run_time, recipients)

        mod.process_event = flaky
        status, body = run(mod)
        self.assertEqual(status, 207)
        self.assertEqual(body["reports_failed"], 1)
        self.assertEqual(body["reports_created"], 2)

    def test_event_cap_limits_research(self):
        mod, s3, processed = build()
        mod.MAX_ITEMS_PER_RUN = 2
        run(mod)
        self.assertEqual(len(processed), 2)

    def test_no_events(self):
        mod, s3, processed = build()
        mod.classify_articles = lambda a: {"events": []}
        status, body = run(mod)
        self.assertEqual(status, 200)
        self.assertEqual(body["events"], [])


if __name__ == "__main__":
    unittest.main()
