from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from model import validate_candidate
from render import render
from store import connect, context, ingest
from watch import run


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.job = self.root / "job"
        (self.job / "evidence").mkdir(parents=True)
        self.started = datetime.now(timezone.utc).isoformat()
        self.day = self.started[:10]
        self.sources = [{"id": "one"}, {"id": "two"}]
        self.db = connect(self.root / "state")
        self.p = {"address": "2 Example Road, Dundee", "postcode": "DD1 1AA", "locality": "Dundee",
                  "bedrooms": 2, "price_gbp": 260000, "price_qualifier": "Offers over", "property_type": "Flat",
                  "agent": "Example Agent", "agent_ref": "ABC1", "url": "https://agent.example/property/abc1",
                  "portal_fallback": False, "fallback_reason": "", "additional_urls": [{"url": "https://www.rightmove.co.uk/properties/1234", "label": "Rightmove"}],
                  "listed_date": None, "listing_kind": "ordinary", "plot_number": "", "notes": "",
                  "prior_report_id": None, "identity_note": "", "source_ids": ["one"], "evidence": []}
        self.text = "2 Example Road, Dundee. Offers over £260,000. 2 bedrooms. For sale."
        self.save_evidence()
        for field, quote in [("address", "2 Example Road, Dundee"), ("price", "Offers over £260,000"), ("bedrooms", "2 bedrooms"), ("availability", "For sale")]:
            self.p["evidence"].append({"file": "evidence/property.json", "field": field, "quote": quote})

    def save_evidence(self):
        (self.job / "evidence/property.json").write_text(json.dumps({"status": 200, "error": "", "url": self.p["url"], "retrieved_at": self.started, "text": self.text}))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def result(self, candidate=None):
        return {"candidates": [candidate or self.p], "source_checks": [{"source_id": "one", "status": "complete", "detail": "Both areas checked", "urls": ["https://agent.example/properties"]}, {"source_id": "two", "status": "failed", "detail": "HTTP 403", "urls": ["https://blocked.example/properties"]}]}

    def ingest(self, run_id, result):
        self.db.execute("INSERT INTO runs(id,started,day,status) VALUES (?,?,?,'running')", (run_id, self.started, self.day))
        self.db.commit()
        return ingest(self.db, result, self.job, self.sources, run_id, self.day, self.started)

    def validate(self, p):
        validate_candidate(p, self.job, {"one", "two"}, set(), datetime.fromisoformat(self.started).date())

    def test_price_bedrooms_evidence_and_availability_are_enforced(self):
        self.validate(self.p)
        for key, value in [("price_gbp", 260001), ("bedrooms", 4), ("url", "javascript:alert(1)"), ("url", "https://www.rightmove.co.uk/property-for-sale/Dundee.html"), ("url", "https://agent.example/uninspected-page"), ("evidence", [])]:
            bad = deepcopy(self.p)
            bad[key] = value
            with self.assertRaises(ValueError):
                self.validate(bad)
        bad = deepcopy(self.p)
        bad["evidence"][0]["quote"] = "A fabricated address"
        with self.assertRaises(ValueError):
            self.validate(bad)
        self.text += " Sold STC."
        self.save_evidence()
        bad = deepcopy(self.p)
        bad["evidence"][-1]["quote"] = "Sold STC"
        with self.assertRaises(ValueError):
            self.validate(bad)

    def test_repeat_and_cross_portal_do_not_reannounce(self):
        self.assertEqual(self.ingest("r1", self.result())["new"], 1)
        p = deepcopy(self.p)
        p["url"] = "https://www.rightmove.co.uk/properties/1234?utm_source=whatever"
        p["agent_ref"] = ""
        p["portal_fallback"] = True
        p["fallback_reason"] = "Agent page temporarily unavailable"
        snapshot = json.loads((self.job / "evidence/property.json").read_text())
        snapshot["url"] = p["url"]
        (self.job / "evidence/property.json").write_text(json.dumps(snapshot))
        result = self.ingest("r2", self.result(p))
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1)

    def test_failed_source_keeps_cutoff_for_catchup(self):
        self.ingest("r1", self.result())
        cutoffs = context(self.db, self.sources)["last_successful_check"]
        self.assertEqual(cutoffs["one"], self.started)
        self.assertIsNone(cutoffs["two"])
        p = deepcopy(self.p)
        p["price_gbp"] = 300000
        self.ingest("r2", self.result(p))
        self.assertEqual(self.db.execute("SELECT status FROM source_checks WHERE run_id='r2' AND source_id='one'").fetchone()[0], "partial")

    def test_missing_source_cannot_be_recorded_as_successful_run(self):
        result = self.result()
        result["source_checks"].pop()
        with self.assertRaises(ValueError):
            self.ingest("bad", result)

    def test_distinct_units_and_legacy_street_hints_are_not_collapsed(self):
        self.db.execute("INSERT INTO legacy(id,data) VALUES ('hint',?)", (json.dumps({"address": "Example Road, Dundee", "price_gbp": 260000}),))
        self.db.commit()
        self.ingest("r1", self.result())
        p = deepcopy(self.p)
        p.update(address="4 Example Road, Dundee", url="https://agent.example/property/abc2", agent_ref="ABC2", additional_urls=[])
        self.text += " 4 Example Road, Dundee."
        self.save_evidence()
        snapshot = json.loads((self.job / "evidence/property.json").read_text())
        snapshot["url"] = p["url"]
        (self.job / "evidence/property.json").write_text(json.dumps(snapshot))
        p["evidence"][0]["quote"] = "4 Example Road, Dundee"
        self.assertEqual(self.ingest("r2", self.result(p))["new"], 1)

    def test_report_has_both_links_and_no_private_check_diagnostics(self):
        self.p["notes"] = '<script>alert("unsafe")</script>'
        self.ingest("r1", self.result())
        html = render(self.db, self.root / "site").read_text()
        self.assertIn("https://agent.example/property/abc1", html)
        self.assertIn("https://www.rightmove.co.uk/properties/1234", html)
        self.assertNotIn("HTTP 403", html)
        self.assertNotIn('<script>alert', html)
        self.assertIn("&lt;script&gt;", html)

    def test_archive_keeps_old_properties_across_empty_days_and_pages(self):
        self.ingest("r1", self.result())
        self.db.execute("UPDATE reports SET day='2020-01-01'")
        for index in range(1, 22):
            p = deepcopy(self.p)
            p["address"] = f"{index + 2} Example Road, Dundee"
            self.db.execute("INSERT INTO properties VALUES (?,?,?,?)", (str(index), json.dumps(p), self.started, self.started))
            self.db.execute("INSERT INTO reports VALUES ('2020-01-02',?,?)", (str(index), json.dumps(p)))
        self.db.execute("INSERT INTO photos VALUES (?,?,?,?)", ('1', 'https://images.example/home.jpg', self.p['url'], self.started))
        self.db.commit()
        html = render(self.db, self.root / "site").read_text()
        older = (self.root / "site/page-2.html").read_text()
        self.assertIn("No new properties to report today", html)
        self.assertIn("22 in the archive", html)
        self.assertEqual(html.count('<article'), 20)
        self.assertEqual(older.count('<article'), 2)
        self.assertIn('href="page-2.html"', html)
        self.assertIn('href="index.html"', older)
        self.assertIn('2 Example Road', older)
        self.assertIn('src="https://images.example/home.jpg"', html)
        self.assertIn('loading="lazy"', html)
        (self.root / "site/page-notes.html").write_text("Unrelated content")
        self.db.execute("DELETE FROM reports WHERE property_id IN ('1','2')")
        self.db.commit()
        render(self.db, self.root / "site")
        self.assertFalse((self.root / "site/page-2.html").exists())
        self.assertTrue((self.root / "site/page-notes.html").exists())

    def test_failed_pages_publication_remains_pending_for_retry(self):
        self.ingest("r1", self.result())
        from watch import publish
        with patch("watch.enrich_photos"), patch("watch.render"), patch("watch.publish_site", side_effect=RuntimeError("Deployment failed")):
            with self.assertRaisesRegex(RuntimeError, "Deployment failed"):
                publish(self.db)
        self.assertIsNone(self.db.execute("SELECT published FROM runs WHERE id='r1'").fetchone()[0])
        with patch("watch.enrich_photos"), patch("watch.render"), patch("watch.publish_site", return_value={"url": "https://example.test/"}):
            publish(self.db)
        self.assertIsNotNone(self.db.execute("SELECT published FROM runs WHERE id='r1'").fetchone()[0])

    def test_scheduled_retry_publishes_pending_report_without_researching_again(self):
        local_day = datetime.now(ZoneInfo("Europe/London")).date().isoformat()
        self.db.execute("INSERT INTO runs(id,started,day,status) VALUES ('pending',?,?,'partial')", (self.started, local_day))
        self.db.commit()
        def mark_published(db):
            with db:
                db.execute("UPDATE runs SET published=? WHERE id='pending'", (self.started,))
        with patch("watch.publish", side_effect=mark_published) as publish, patch("watch.research") as research:
            run(self.db, self.root / "state", True, scheduled=True)
            publish.assert_called_once()
            research.assert_not_called()


if __name__ == "__main__":
    unittest.main()
