"""Explicit reconciliation repairs verified aliases without relaxing dedup rules."""
import json
import unittest

from reconcile import validate_proof
from store import reconcile_current_aliases
from test_identity import MissingPostcodeTests


class ReconcileTests(MissingPostcodeTests):
    def duplicate_pair(self):
        for p in (self.agent, self.portal):
            p["address"] = "Heather Gardens, Dundee"
            p["additional_urls"] = []
            path = self.job / p["evidence"][0]["file"]
            path.write_text(path.read_text().replace("16 Heather Gardens", "Heather Gardens"))
            for item in p["evidence"]:
                item["quote"] = item["quote"].replace("16 Heather Gardens", "Heather Gardens")
        counts = self.ingest("one", [self.agent, self.portal])
        self.assertEqual(counts["new"], 2)
        self.db.execute("UPDATE runs SET published='verified'")
        self.db.commit()
        return [self.agent["url"], self.portal["url"]]

    def test_verified_pair_preserves_links_history_and_successful_photo(self):
        urls = self.duplicate_pair()
        photo_id = self.db.execute("SELECT property_id FROM aliases WHERE alias=?", ("url:" + urls[1],)).fetchone()[0]
        self.db.execute("INSERT INTO photos VALUES (?,?,?,?)", (photo_id, "https://example.org/photo.jpg", urls[1], self.started))
        self.db.commit()
        self.assertTrue(reconcile_current_aliases(self.db, "one", urls))
        self.assertFalse(reconcile_current_aliases(self.db, "one", urls))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM properties").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1)
        self.assertIsNone(self.db.execute("SELECT published FROM runs").fetchone()[0])
        archived = json.loads(self.db.execute("SELECT data FROM reports").fetchone()[0])
        self.assertEqual(archived["price_gbp"], 170000)
        self.assertFalse(archived["portal_fallback"])
        self.assertEqual(archived["additional_urls"][0]["url"], urls[1])
        self.assertEqual(self.db.execute("SELECT image_url FROM photos").fetchone()[0], "https://example.org/photo.jpg")

    def test_earlier_archive_entry_is_protected(self):
        urls = self.duplicate_pair()
        self.db.execute("UPDATE reports SET day='2020-01-01' WHERE property_id=(SELECT property_id FROM aliases WHERE alias=?)", ("url:" + urls[1],))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "earlier historical"):
            reconcile_current_aliases(self.db, "one", urls)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM properties").fetchone()[0], 2)

    def test_conflicting_facts_are_protected(self):
        urls = self.duplicate_pair()
        row = self.db.execute("SELECT id,data FROM properties WHERE id=(SELECT property_id FROM aliases WHERE alias=?)", ("url:" + urls[1],)).fetchone()
        data = json.loads(row["data"])
        data["bedrooms"] = 3
        self.db.execute("UPDATE properties SET data=? WHERE id=?", (json.dumps(data), row["id"]))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "Conflicting property facts"):
            reconcile_current_aliases(self.db, "one", urls)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 2)

    def test_proof_requires_current_accessible_matching_saved_content(self):
        worker = self.job / "workers" / "worker-1"
        worker.mkdir(parents=True)
        urls = [self.agent["url"], self.portal["url"]]
        quote = "A distinctive property description with a separate utility room and uninterrupted views over the river and adjoining park."
        evidence = []
        for index, url in enumerate(urls):
            name = f"page-{index}.json"
            (worker / name).write_text(json.dumps(dict(url=url, status=200, text=quote, retrieved_at=self.started)))
            evidence.append(dict(file=name, quote=quote))
        proof = worker / "proof.json"
        proof.write_text(json.dumps(dict(urls=urls, evidence=evidence)))
        self.assertEqual(validate_proof(proof, self.job, self.started), urls)
        bad = json.loads((worker / "page-1.json").read_text())
        bad["status"] = 403
        (worker / "page-1.json").write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, "not accessible"):
            validate_proof(proof, self.job, self.started)
        bad["status"] = 200
        bad["text"] = "A different home on the same street"
        (worker / "page-1.json").write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, "distinctive shared"):
            validate_proof(proof, self.job, self.started)


if __name__ == "__main__":
    unittest.main()
