"""Regression for independent portal/agent discoveries without postcodes."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from model import same_addressed_home
from store import connect, ingest


class MissingPostcodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = connect(self.root / "state")
        self.started = datetime.now(timezone.utc).isoformat()
        self.day = self.started[:10]
        self.job = self.root / "job"
        (self.job / "evidence").mkdir(parents=True)
        self.agent = dict(address="16 Heather Gardens, Dundee", postcode="", locality="Dundee",
                          price_gbp=170000, bedrooms=2, property_type="Semi-detached house",
                          agent="Axis Property", agent_ref="22138669", price_qualifier="Offers over",
                          url="https://www.axispropertygroup.co.uk/properties/22138669/sales",
                          portal_fallback=False, fallback_reason="", listed_date=None,
                          listing_kind="ordinary", plot_number="", notes="", prior_report_id=None,
                          identity_note="", source_ids=["axis"], evidence=[],
                          additional_urls=[{"label": "Zoopla", "url": "https://www.zoopla.co.uk/for-sale/details/74299189/"}])
        self.portal = deepcopy(self.agent)
        self.portal.update(url="https://www.rightmove.co.uk/properties/93405096", portal_fallback=True,
                           fallback_reason="Original URL not found by this worker", additional_urls=[],
                           agent_ref="85df0d2c-9121-4f40-b75c-801816d8ecd4", source_ids=["rightmove"], listed_date=self.day)
        for name, p in (("agent", self.agent), ("portal", self.portal)):
            quotes = {"address": p["address"], "price": "Offers over £170,000", "bedrooms": "2 bedrooms",
                      "availability": "Semi-detached house for sale"}
            if p["listed_date"]:
                quotes["date"] = "Listed " + p["listed_date"]
            evidence = "evidence/" + name + ".json"
            (self.job / evidence).write_text(json.dumps(dict(url=p["url"], status=200, error="", retrieved_at=self.started, text=". ".join(quotes.values()))))
            p["evidence"] = [dict(file=evidence, field=field, quote=quote) for field, quote in quotes.items()]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def ingest(self, run_id, candidates, day=None):
        day = day or self.day
        self.db.execute("INSERT INTO runs(id,started,day,status) VALUES (?,?,?,'running')", (run_id, self.started, day))
        self.db.commit()
        sources = [{"id": "axis"}, {"id": "rightmove"}]
        result = {"candidates": deepcopy(candidates), "source_checks": [dict(source_id=s["id"], status="complete", detail="Both localities checked", urls=[]) for s in sources]}
        return ingest(self.db, result, self.job, sources, run_id, day, self.started)

    def assert_one_complete_property(self):
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM properties").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1)
        p = json.loads(self.db.execute("SELECT data FROM properties").fetchone()[0])
        self.assertEqual(p["url"], self.agent["url"])
        self.assertFalse(p["portal_fallback"])
        self.assertEqual(p["agent_ref"], self.agent["agent_ref"])
        self.assertEqual(p["listed_date"], self.day)
        self.assertEqual({link["url"] for link in p["additional_urls"]}, {self.portal["url"], self.agent["additional_urls"][0]["url"]})
        archived = json.loads(self.db.execute("SELECT data FROM reports").fetchone()[0])
        self.assertEqual(archived["url"], self.agent["url"])
        self.assertEqual(archived["additional_urls"], p["additional_urls"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM aliases WHERE alias LIKE 'ref:%'").fetchone()[0], 2)

    def test_parallel_portal_then_original_is_one_property(self):
        counts = self.ingest("one", [self.portal, self.agent])
        self.assertEqual((counts["new"], counts["duplicates"], counts["rejected"]), (1, 1, 0))
        self.assert_one_complete_property()

    def test_reverse_worker_order_keeps_original_and_portal(self):
        counts = self.ingest("one", [self.agent, self.portal])
        self.assertEqual((counts["new"], counts["duplicates"], counts["rejected"]), (1, 1, 0))
        self.assert_one_complete_property()

    def test_later_day_portal_discovery_does_not_reannounce(self):
        self.ingest("one", [self.agent], day="2020-01-01")
        counts = self.ingest("two", [self.portal])
        self.assertEqual((counts["new"], counts["duplicates"]), (0, 1))
        self.assert_one_complete_property()
        self.assertEqual(self.db.execute("SELECT day FROM reports").fetchone()[0], "2020-01-01")
        self.assertEqual(self.ingest("three", [self.agent, self.portal])["new"], 0)

    def test_link_enrichment_preserves_original_report_price(self):
        self.ingest("one", [self.agent], day="2020-01-01")
        self.portal["price_gbp"] = 160000
        evidence = self.job / "evidence/portal.json"
        evidence.write_text(evidence.read_text().replace("170,000", "160,000"))
        for item in self.portal["evidence"]:
            item["quote"] = item["quote"].replace("170,000", "160,000")
        counts = self.ingest("two", [self.portal])
        self.assertEqual(counts["new"], 0)
        archived = json.loads(self.db.execute("SELECT data FROM reports").fetchone()[0])
        self.assertEqual(archived["price_gbp"], 170000)
        self.assertIn(self.portal["url"], [link["url"] for link in archived["additional_urls"]])

    def test_missing_postcode_can_match_known_postcode_but_conflicts_cannot(self):
        other = deepcopy(self.agent)
        other["postcode"] = "DD3 0AA"
        self.assertTrue(same_addressed_home(self.agent, other))
        self.agent["postcode"] = "DD3 0AB"
        self.assertFalse(same_addressed_home(self.agent, other))

    def test_full_address_needs_corroboration_and_cannot_merge_other_homes(self):
        for key, value in [("address", "18 Heather Gardens, Dundee"), ("address", "16A Heather Gardens, Dundee"),
                           ("agent", "Another Agent"), ("bedrooms", 3), ("locality", "Broughty Ferry"), ("plot_number", "2")]:
            with self.subTest(key=key, value=value):
                other = {**self.agent, key: value}
                self.assertFalse(same_addressed_home(self.agent, other))
        for address in ["Heather Gardens, Dundee", "Flat A, Heather Gardens, Dundee"]:
            p = {**self.agent, "address": address}
            self.assertFalse(same_addressed_home(p, p))

    def test_flats_need_explicit_units_and_preserve_unit_separators(self):
        p = {**self.agent, "property_type": "Flat"}
        self.assertFalse(same_addressed_home(p, p))
        for address in ["Flat at 16 Heather Gardens, Dundee", "Flat in 16 Heather Gardens, Dundee",
                        "Flat 2, Heather Gardens, Dundee", "Flat A, Heather Gardens, Dundee DD3"]:
            ambiguous = {**p, "address": address}
            self.assertFalse(same_addressed_home(ambiguous, ambiguous), address)
        for address in ["Flat A, 16 Heather Gardens, Dundee", "16A Heather Gardens, Dundee", "1/23 Heather Gardens, Dundee"]:
            p["address"] = address
            self.assertTrue(same_addressed_home(p, p))
        self.assertFalse(same_addressed_home(p, {**p, "address": "12/3 Heather Gardens, Dundee"}))
        self.assertFalse(same_addressed_home({**p, "address": "Flat A, 16 Heather Gardens, Dundee"}, {**p, "address": "Flat B, 16 Heather Gardens, Dundee"}))
