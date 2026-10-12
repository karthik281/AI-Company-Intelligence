"""
Unit tests for orchestrator/taxonomy.py and the multi-user helpers in
orchestrator/lambda_function.py (query building, sent log, one-per-user).

Run directly: python3 tests/test_taxonomy.py
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "orchestrator"))

import taxonomy as t  # noqa: E402


def user(email, roles=("founder",), regions=None, industries=None, **kw):
    return t.normalise_profile({
        "email": email, "roles": list(roles),
        "regions": regions, "industries": industries, **kw,
    })


def event(company, kind=t.KIND_STARTUP, region=None, industry=None,
          round_="Seed"):
    return t.normalise_event_tags({
        "company": company, "kind": kind, "region": region,
        "industry": industry, "round": round_,
    })


class ProfileTests(unittest.TestCase):

    def test_unknown_values_dropped_and_ordered(self):
        p = t.normalise_profile({
            "email": " A@X.com ", "roles": ["Investor", "wizard", "FOUNDER"],
            "regions": ["apac", "Mars", "EMEA"],
            "industries": ["fintech"],
        })
        self.assertEqual(p["email"], "a@x.com")
        self.assertEqual(p["roles"], ["founder", "investor"])
        self.assertEqual(p["regions"], ["EMEA", "APAC"])
        self.assertEqual(p["industries"], ["Fintech"])

    def test_empty_means_all_but_role_defaults_to_founder(self):
        p = t.normalise_profile({"email": "a@x.com"})
        self.assertEqual(p["roles"], ["founder"])
        self.assertEqual(p["regions"], t.ALL_REGIONS)
        self.assertEqual(p["industries"], t.ALL_INDUSTRIES)

    def test_validate_profile_input(self):
        ok = {"roles": ["founder"], "regions": ["EMEA"], "industries": ["Fintech"]}
        self.assertEqual(t.validate_profile_input(ok), [])
        bad = t.validate_profile_input(
            {"roles": [], "regions": ["Mars"], "industries": None}
        )
        self.assertEqual(len(bad), 3, bad)


class MatchTests(unittest.TestCase):

    def test_role_decides_kind(self):
        f = user("f@x.com", roles=["founder"])
        i = user("i@x.com", roles=["investor"])
        b = user("b@x.com", roles=["founder", "investor"])
        startup = event("S")
        fund = event("Fund", kind=t.KIND_INVESTOR)
        self.assertTrue(t.match_score(f, startup))
        self.assertFalse(t.match_score(f, fund))
        self.assertFalse(t.match_score(i, startup))
        self.assertTrue(t.match_score(i, fund))
        self.assertTrue(t.match_score(b, startup) and t.match_score(b, fund))

    def test_region_and_industry_filters(self):
        u = user("u@x.com", regions=["EMEA"], industries=["Fintech"])
        self.assertTrue(t.match_score(u, event("a", region="EMEA", industry="Fintech")))
        self.assertFalse(t.match_score(u, event("b", region="APAC", industry="Fintech")))
        self.assertFalse(t.match_score(u, event("c", region="EMEA", industry="Edtech")))

    def test_untagged_and_global_match_anyone(self):
        u = user("u@x.com", regions=["EMEA"], industries=["Fintech"])
        self.assertTrue(t.match_score(u, event("a")))
        self.assertTrue(t.match_score(u, event("b", region=t.GLOBAL_REGION,
                                               industry="Fintech")))

    def test_exact_match_outranks_wildcard(self):
        u = user("u@x.com", regions=["EMEA"], industries=["Fintech"])
        exact = t.match_score(u, event("a", region="EMEA", industry="Fintech"))
        wild = t.match_score(u, event("b"))
        self.assertGreater(exact, wild)

    def test_inactive_user_gets_nothing(self):
        u = user("u@x.com", active=False)
        self.assertEqual(t.match_score(u, event("a")), 0)


class AssignTests(unittest.TestCase):

    def test_one_event_per_user_and_skips_sent(self):
        u = user("u@x.com")
        a, b = event("A", region="EMEA"), event("B", region="EMEA")
        got = t.assign_events([u], [a, b], {}, 10)
        self.assertEqual(list(got), ["u@x.com"])
        first = got["u@x.com"]
        again = t.assign_events(
            [u], [a, b], {"u@x.com": {t.event_key(first)}}, 10
        )
        self.assertNotEqual(again["u@x.com"]["company"], first["company"])
        none_left = t.assign_events(
            [u], [a, b],
            {"u@x.com": {t.event_key(a), t.event_key(b)}}, 10,
        )
        self.assertEqual(none_left, {})

    def test_users_share_events_to_limit_research(self):
        users = [user(f"u{i}@x.com") for i in range(5)]
        events = [event(f"E{i}") for i in range(5)]
        got = t.assign_events(users, events, {}, 10)
        distinct = {t.event_key(e) for e in got.values()}
        self.assertEqual(len(got), 5)
        self.assertEqual(len(distinct), 1)

    def test_cap_on_distinct_events(self):
        users = [
            user("a@x.com", regions=["EMEA"]),
            user("b@x.com", regions=["APAC"]),
            user("c@x.com", regions=["LATAM"]),
        ]
        events = [
            event("E", region="EMEA"),
            event("A", region="APAC"),
            event("L", region="LATAM"),
        ]
        got = t.assign_events(users, events, {}, 2)
        self.assertEqual(len({t.event_key(e) for e in got.values()}), 2)
        self.assertEqual(len(got), 2)

    def test_users_only_get_events_in_their_region(self):
        users = [user("eu@x.com", regions=["EMEA"]),
                 user("ap@x.com", regions=["APAC"])]
        events = [event("Berlin", region="EMEA"), event("Pune", region="APAC")]
        got = t.assign_events(users, events, {}, 10)
        self.assertEqual(got["eu@x.com"]["company"], "Berlin")
        self.assertEqual(got["ap@x.com"]["company"], "Pune")


class OrchestratorHelperTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from test_recipients import load_orchestrator, FakeS3
        cls.load, cls.FakeS3 = staticmethod(load_orchestrator), FakeS3

    def test_queries_follow_profiles_and_are_capped(self):
        mod = self.load({"MAX_NEWS_QUERIES": "8"}, self.FakeS3())
        users = [user("a@x.com", roles=["founder", "investor"],
                      regions=["EMEA", "LATAM"], industries=["Fintech", "Edtech"])]
        queries = mod.build_news_queries(users)
        self.assertEqual(len(queries), 8)
        self.assertTrue(any("Europe" in q for q in queries))
        self.assertTrue(any("venture capital" in q for q in queries))
        self.assertFalse(any("India" in q for q in queries), queries)

    def test_founder_only_gets_no_investor_queries(self):
        mod = self.load({}, self.FakeS3())
        queries = mod.build_news_queries([user("a@x.com", roles=["founder"])])
        self.assertFalse(any("venture capital" in q for q in queries))

    def test_get_users_from_s3_json_and_legacy_fallback(self):
        import json
        s3 = self.FakeS3({"config/users.json": json.dumps([
            {"email": "A@x.com", "roles": ["investor"], "regions": ["APAC"]},
            {"email": "off@x.com", "active": False},
            {"email": "not-an-email"},
        ]).encode()})
        mod = self.load({}, s3)
        users = mod.get_users()
        self.assertEqual([u["email"] for u in users], ["a@x.com"])
        self.assertEqual(users[0]["regions"], ["APAC"])

        legacy = self.load({}, self.FakeS3(
            {"config/recipients.txt": b"one@x.com\ntwo@x.com\n"}))
        self.assertEqual(
            sorted(u["email"] for u in legacy.get_users()),
            ["one@x.com", "two@x.com"],
        )

    def test_sent_log_round_trip(self):
        class RWS3(self.FakeS3):
            def put_object(self, Bucket, Key, Body, ContentType):
                self.objects[Key] = Body

        mod = self.load({}, RWS3())
        self.assertEqual(mod.load_sent("a@x.com"), set())
        mod.record_sent("a@x.com", "k1")
        mod.record_sent("a@x.com", "k2")
        self.assertEqual(mod.load_sent("A@x.com"), {"k1", "k2"})
        self.assertNotIn("a@x.com", mod.sent_log_key("a@x.com"))


if __name__ == "__main__":
    unittest.main()
