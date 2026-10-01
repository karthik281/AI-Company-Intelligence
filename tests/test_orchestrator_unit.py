"""
Unit tests for the pure/business-logic functions in
orchestrator/lambda_function.py (no AWS calls).

Run directly: python3 tests/test_orchestrator_unit.py
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORCH_DIR = os.path.join(ROOT, "orchestrator")

os.environ.setdefault("BEDROCK_MODEL_ID", "test-model")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("SES_SENDER_EMAIL", "sender@example.com")
# boto3.client() needs a resolvable region even just to construct a client
# object (no network call happens at import time, but it still validates).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

sys.path.insert(0, ORCH_DIR)
import lambda_function as orch  # noqa: E402


class UrlHelpersTests(unittest.TestCase):

    def test_normalise_url_strips_tracking_params(self):
        a = orch.normalise_url("https://Example.com/Path/?utm_source=x&id=1")
        b = orch.normalise_url("https://example.com/Path?id=1")
        self.assertEqual(a, b)

    def test_normalise_url_strips_www_and_trailing_slash(self):
        self.assertEqual(
            orch.normalise_url("https://www.Example.com/foo/"),
            orch.normalise_url("https://example.com/foo"),
        )

    def test_normalise_url_empty(self):
        self.assertEqual(orch.normalise_url(""), "")
        self.assertEqual(orch.normalise_url(None), "")

    def test_domain_of(self):
        self.assertEqual(orch.domain_of("https://www.Example.com/x"), "example.com")
        self.assertEqual(orch.domain_of(""), "")


class SmallHelpersTests(unittest.TestCase):

    def test_is_missing(self):
        for v in (None, "", "  ", "N/A", "none", "Not Disclosed", [], {}):
            self.assertTrue(orch.is_missing(v), v)
        for v in ("AJVC", "0", 0, ["x"]):
            self.assertFalse(orch.is_missing(v), v)

    def test_safe_name(self):
        self.assertEqual(orch.safe_name("Unveilr AI!"), "Unveilr_AI")
        self.assertEqual(orch.safe_name(""), "unknown")
        self.assertEqual(orch.safe_name(None), "unknown")

    def test_money_key(self):
        self.assertEqual(orch.money_key("Rs 16.7 Cr"), "16.7")
        self.assertEqual(orch.money_key(None), "")


class ReconcileEventTests(unittest.TestCase):

    def test_fills_missing_fields_from_matching_research_record(self):
        event = {
            "company": "Acme", "round": "Pre-Seed", "source_title": "x",
        }
        packet = {"funding": [{
            "round": "pre-seed", "amount": "Not publicly disclosed",
            "valuation": "$5M", "lead_investor": "Acme Capital",
            "date": "2026-01-01", "other_investors": ["X"],
            "source_urls": ["https://x.com"],
        }]}

        result = orch.reconcile_event(event, packet)

        self.assertEqual(result["valuation"], "$5M")
        self.assertEqual(result["lead_investor"], "Acme Capital")
        self.assertEqual(result["event_date"], "2026-01-01")
        self.assertEqual(result["investors"], ["X"])
        self.assertIn("valuation taken from web research", result["reconciliation"])

    def test_amount_equal_to_valuation_is_corrected(self):
        event = {
            "company": "Acme", "amount": "Rs 16.7 Cr", "valuation": "Rs 16.7 Cr",
            "source_title": "Acme raises funding",
        }
        result = orch.reconcile_event(event, {"funding": []})
        self.assertEqual(result["amount"], orch.NOT_DISCLOSED)
        self.assertEqual(result["valuation"], "Rs 16.7 Cr")

    def test_valuation_only_headline_is_corrected(self):
        event = {
            "company": "Acme", "amount": "Rs 16.7 Cr",
            "source_title": "Acme raises funding at Rs 16.7 Cr valuation",
        }
        result = orch.reconcile_event(event, {"funding": []})
        self.assertEqual(result["amount"], orch.NOT_DISCLOSED)
        self.assertEqual(result["valuation"], "Rs 16.7 Cr")

    def test_defaults_to_not_disclosed_with_no_research(self):
        event = {"company": "Acme", "source_title": "x"}
        result = orch.reconcile_event(event, {"funding": []})
        for field in ("amount", "valuation", "lead_investor", "event_date"):
            self.assertEqual(result[field], orch.NOT_DISCLOSED)

    def test_does_not_mutate_input_event(self):
        event = {"company": "Acme", "source_title": "x"}
        orch.reconcile_event(event, {"funding": []})
        self.assertNotIn("reconciliation", event)


class MergeSourcesTests(unittest.TestCase):

    def test_grounded_sources_marked_verified_and_deduped(self):
        grounded = [{"id": "S1", "url": "https://a.com/x", "domain": "a.com"}]
        packet = {"sources": [{"url": "https://a.com/x/"}],  # same URL, trailing slash
                  "claims": [{"source_urls": ["https://b.com/y"]}]}

        register = orch.merge_sources(packet, grounded)

        by_url = {orch.normalise_url(s["url"]): s for s in register}
        self.assertTrue(by_url[orch.normalise_url("https://a.com/x")]["verified"])
        self.assertFalse(by_url[orch.normalise_url("https://b.com/y")]["verified"])
        self.assertEqual(len(register), 2)

    def test_empty_inputs(self):
        self.assertEqual(orch.merge_sources({}, []), [])


class ValidateResearchSourcesTests(unittest.TestCase):

    def test_flags_urls_not_in_grounded_set(self):
        grounded = [{"url": "https://a.com"}]
        packet = {"claims": [{"source_urls": ["https://a.com", "https://evil.com"]}]}
        problems = orch.validate_research_sources(packet, grounded)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["url"], "https://evil.com")

    def test_no_problems_when_all_urls_grounded(self):
        grounded = [{"url": "https://a.com"}]
        packet = {"claims": [{"source_urls": ["https://a.com"]}]}
        self.assertEqual(orch.validate_research_sources(packet, grounded), [])


class AttachSourceIdsTests(unittest.TestCase):

    def test_attaches_matching_ids_only(self):
        sources = [{"id": "S1", "url": "https://a.com", "domain": "a.com", "verified": True}]
        packet = {"claims": [{"source_urls": ["https://a.com", "https://unknown.com"]}]}

        with_ids, numbered = orch.attach_source_ids(packet, sources)

        self.assertEqual(with_ids["claims"][0]["source_ids"], ["S1"])
        self.assertEqual(numbered[0]["id"], "S1")


class BuildWarningsTests(unittest.TestCase):

    def test_warns_when_no_sources_grounded(self):
        web_research = {"sources": [{"verified": False}], "grounded_source_count": 0,
                         "research_meta": {}}
        warnings = orch.build_warnings(web_research, {}, "end_turn")
        self.assertTrue(any("web search" in w for w in warnings))

    def test_warns_on_downgraded_records(self):
        web_research = {"sources": [{"verified": True}], "grounded_source_count": 1,
                         "research_meta": {"downgraded_records": 2}}
        warnings = orch.build_warnings(web_research, {}, "end_turn")
        self.assertTrue(any("downgraded" in w for w in warnings))

    def test_warns_on_truncated_report(self):
        web_research = {"sources": [], "grounded_source_count": 0, "research_meta": {}}
        warnings = orch.build_warnings(web_research, {}, "max_tokens")
        self.assertTrue(any("output limit" in w for w in warnings))

    def test_no_warnings_on_clean_run(self):
        web_research = {"sources": [{"verified": True}], "grounded_source_count": 1,
                         "research_meta": {}}
        warnings = orch.build_warnings(web_research, {"reconciliation": []}, "end_turn")
        self.assertEqual(warnings, [])


class EventMarkerKeyTests(unittest.TestCase):

    def test_deterministic_and_safe(self):
        key1 = orch.event_marker_key({"company": "Acme Inc.", "round": "Pre-Seed"})
        key2 = orch.event_marker_key({"company": "Acme Inc.", "round": "Pre-Seed"})
        self.assertEqual(key1, key2)
        self.assertTrue(key1.startswith("state/reported/"))
        self.assertTrue(key1.endswith(".json"))


class BuildKeyFactsTests(unittest.TestCase):

    def test_includes_core_facts_and_other_investors(self):
        event = {
            "round": "Seed", "amount": "$1M", "valuation": "$10M",
            "lead_investor": "A", "investors": ["A", "B"],
            "event_date": "2026-01-15", "supporting_urls": ["https://news.com/x"],
        }
        facts = dict(orch.build_key_facts(event))
        self.assertEqual(facts["Amount raised"], "$1M")
        self.assertEqual(facts["Other investors"], "B")
        self.assertEqual(facts["Announced"], "15 Jan 2026")


if __name__ == "__main__":
    unittest.main(verbosity=2)
