"""
Unit tests for the pure/business-logic functions in
research/lambda_function.py (no AWS calls).

Run directly: python3 tests/test_research_unit.py
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH_DIR = os.path.join(ROOT, "research")

os.environ.setdefault("BEDROCK_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

sys.path.insert(0, RESEARCH_DIR)
import lambda_function as research  # noqa: E402


class NormaliseUrlTests(unittest.TestCase):

    def test_strips_www_tracking_and_trailing_slash(self):
        a = research.normalise_url("https://www.Example.com/x/?utm_source=y")
        b = research.normalise_url("https://example.com/x")
        self.assertEqual(a, b)

    def test_empty(self):
        self.assertEqual(research.normalise_url(""), "")


class ExtractGroundedOutputTests(unittest.TestCase):

    def test_text_and_citations_in_separate_blocks(self):
        response = {
            "stopReason": "end_turn",
            "usage": {},
            "output": {"message": {"content": [
                {"text": "Fact one."},
                {"citationsContent": {"citations": [
                    {"location": {"web": {"url": "https://a.com", "domain": "a.com"}}}
                ]}},
            ]}},
        }
        out = research.extract_grounded_output(response)
        self.assertIn("Fact one.", out["text"])
        self.assertIn("[S1]", out["text"])
        self.assertEqual(len(out["sources"]), 1)
        self.assertEqual(out["sources"][0]["url"], "https://a.com")

    def test_duplicate_urls_reuse_the_same_source_id(self):

        def citation(url):
            return {"location": {"web": {"url": url, "domain": "a.com"}}}

        block_a = {"text": "A.",
                   "citationsContent": {"citations": [citation("https://a.com/x")]}}
        # Same URL with a trailing slash -> should reuse S1, not mint S2.
        block_b = {"text": "B.",
                   "citationsContent": {"citations": [citation("https://a.com/x/")]}}

        response = {
            "stopReason": "end_turn",
            "usage": {},
            "output": {"message": {"content": [block_a, block_b]}},
        }

        out = research.extract_grounded_output(response)
        self.assertEqual(len(out["sources"]), 1)
        self.assertEqual(out["text"].count("[S1]"), 2)

    def test_no_citations_returns_empty_sources(self):
        response = {"stopReason": "end_turn", "usage": {},
                     "output": {"message": {"content": [{"text": "Just text."}]}}}
        out = research.extract_grounded_output(response)
        self.assertEqual(out["sources"], [])
        self.assertEqual(out["text"], "Just text.")


class CleanJsonResponseTests(unittest.TestCase):

    def test_strips_markdown_fence(self):
        text = '```json\n{"a": 1}\n```'
        self.assertEqual(research.clean_json_response(text), '{"a": 1}')

    def test_isolates_object_from_surrounding_commentary(self):
        text = 'Here you go:\n{"a": 1}\nHope that helps.'
        self.assertEqual(research.clean_json_response(text), '{"a": 1}')

    def test_raises_on_empty(self):
        with self.assertRaises(Exception):
            research.clean_json_response("")

    def test_raises_on_truncated_object(self):
        with self.assertRaises(Exception):
            research.clean_json_response('{"a": 1, "b": [1, 2')

    def test_raises_when_no_object_at_all(self):
        with self.assertRaises(Exception):
            research.clean_json_response("no json here")


class ValidateResearchPacketTests(unittest.TestCase):

    def _valid_packet(self):
        return {
            "company": "Acme", "claims": [], "customers": [], "technology": [],
            "competitors": [], "funding": [], "unknowns": [],
        }

    def test_accepts_valid_packet(self):
        packet = self._valid_packet()
        self.assertIs(research.validate_research_packet(packet), packet)

    def test_defaults_missing_executive_facts_to_list(self):
        packet = self._valid_packet()
        self.assertEqual(packet.get("executive_facts"), None)
        result = research.validate_research_packet(packet)
        self.assertEqual(result["executive_facts"], [])

    def test_rejects_non_dict(self):
        with self.assertRaises(Exception):
            research.validate_research_packet(["not", "a", "dict"])

    def test_rejects_missing_company(self):
        packet = self._valid_packet()
        del packet["company"]
        with self.assertRaises(Exception):
            research.validate_research_packet(packet)

    def test_rejects_missing_required_list(self):
        packet = self._valid_packet()
        del packet["funding"]
        with self.assertRaises(Exception):
            research.validate_research_packet(packet)

    def test_rejects_wrong_type_for_required_list(self):
        packet = self._valid_packet()
        packet["funding"] = "not a list"
        with self.assertRaises(Exception):
            research.validate_research_packet(packet)


class AttachSourceUrlsTests(unittest.TestCase):

    def test_maps_valid_ids_to_urls(self):
        sources = [{"id": "S1", "url": "https://a.com", "domain": "a.com"}]
        packet = {"claims": [
            {"source_ids": ["S1"], "status": "CONFIRMED"},
        ], "customers": [], "technology": [], "competitors": [], "funding": [],
            "executive_facts": []}

        packet, stats = research.attach_source_urls(packet, sources)

        self.assertEqual(packet["claims"][0]["source_urls"], ["https://a.com"])
        self.assertEqual(stats["dropped_ids"], 0)
        self.assertEqual(stats["downgraded"], 0)
        self.assertTrue(packet["sources"][0]["cited_in_packet"])

    def test_drops_unknown_ids_and_downgrades_unsourced_confirmed(self):
        sources = [{"id": "S1", "url": "https://a.com", "domain": "a.com"}]
        packet = {"claims": [
            {"source_ids": ["S9"], "status": "CONFIRMED", "evidence": "x"},
        ], "customers": [], "technology": [], "competitors": [], "funding": [],
            "executive_facts": []}

        packet, stats = research.attach_source_urls(packet, sources)

        self.assertEqual(packet["claims"][0]["source_ids"], [])
        self.assertEqual(packet["claims"][0]["status"], "INFERRED")
        self.assertEqual(packet["claims"][0]["confidence"], "LOW")
        self.assertEqual(stats["dropped_ids"], 1)
        self.assertEqual(stats["downgraded"], 1)
        self.assertFalse(packet["sources"][0]["cited_in_packet"])

    def test_non_confirmed_record_with_no_source_is_not_downgraded(self):
        sources = [{"id": "S1", "url": "https://a.com", "domain": "a.com"}]
        packet = {"claims": [
            {"source_ids": [], "status": "UNKNOWN"},
        ], "customers": [], "technology": [], "competitors": [], "funding": [],
            "executive_facts": []}

        packet, stats = research.attach_source_urls(packet, sources)

        self.assertEqual(packet["claims"][0]["status"], "UNKNOWN")
        self.assertEqual(stats["downgraded"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
