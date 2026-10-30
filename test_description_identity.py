"""Street-only cross-portal matches require exact, substantial listing prose."""
from copy import deepcopy
import json
import unittest

from identity_evidence import description_fingerprints
import test_identity


DESCRIPTION = """We are pleased to offer a home on Heather Gardens with a distinctive
split-level entrance and a rear conservatory overlooking a mature walled garden.
The ground floor has a spacious living room with an original tiled fireplace,
separate dining room and a kitchen with a walk-in pantry beside the back door.
Upstairs the main bedroom overlooks the garden while the second bedroom faces
the street. A separate study beside the bathroom provides useful working space.
The detached garage has access from the lane and includes a workshop with its
own side entrance. Outside, a paved path leads past established apple trees to
a greenhouse at the far end of the garden. The property also has solar panels,
double glazing and generous storage beneath the stairs."""


class DescriptionIdentityTests(unittest.TestCase):
    setUp = test_identity.MissingPostcodeTests.setUp
    tearDown = test_identity.MissingPostcodeTests.tearDown
    ingest = test_identity.MissingPostcodeTests.ingest

    def pair(self):
        pair = [deepcopy(self.agent), deepcopy(self.portal)]
        for index, p in enumerate(pair):
            p.update(address="Heather Gardens, Dundee", agent_ref="", additional_urls=[],
                     portal_fallback=True, fallback_reason="Original not located")
            p["url"] = ("https://www.rightmove.co.uk/properties/11111" if index == 0
                        else "https://www.primelocation.com/for-sale/details/22222/")
            path = self.job / p["evidence"][0]["file"]
            snapshot = json.loads(path.read_text())
            snapshot["url"] = p["url"]
            snapshot["text"] = snapshot["text"].replace("16 Heather Gardens", "Heather Gardens")
            heading = " Description " if index == 0 else " About this property Private garden Central heating "
            snapshot["text"] += heading + DESCRIPTION + " Read full description Mortgage calculator"
            path.write_text(json.dumps(snapshot))
            for item in p["evidence"]:
                item["quote"] = item["quote"].replace("16 Heather Gardens", "Heather Gardens")
        return pair

    def test_cross_portal_both_orders(self):
        pair = self.pair()
        self.assertEqual(description_fingerprints(pair[0], self.job)[0][0],
                         description_fingerprints(pair[1], self.job)[0][0])
        counts = self.ingest("one", list(reversed(pair)))
        self.assertEqual((counts["new"], counts["duplicates"]), (1, 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM aliases").fetchone()[0], 2)

    def test_later_run_matches_persisted_description(self):
        pair = self.pair()
        self.ingest("one", pair[:1], day="2020-01-01")
        self.assertEqual(self.ingest("two", pair[1:])["new"], 0)
        self.assertEqual(self.db.execute("SELECT day FROM reports").fetchone()[0], "2020-01-01")

    def test_different_description_stays_separate(self):
        pair = self.pair()
        path = self.job / pair[1]["evidence"][0]["file"]
        path.write_text(path.read_text().replace("apple trees", "pear trees"))
        self.assertEqual(self.ingest("one", pair)["new"], 2)

    def test_conflicting_postcodes_stay_separate(self):
        pair = self.pair()
        pair[0]["postcode"], pair[1]["postcode"] = "DD3 0AA", "DD3 0AB"
        self.assertEqual(self.ingest("one", pair)["new"], 2)

    def test_same_portal_does_not_match_cloned_copy(self):
        pair = self.pair()
        path = self.job / pair[1]["evidence"][0]["file"]
        snapshot = json.loads(path.read_text())
        pair[1]["url"] = snapshot["url"] = "https://www.rightmove.co.uk/properties/22222"
        snapshot["text"] = snapshot["text"].replace("About this property Private garden Central heating", "Description")
        path.write_text(json.dumps(snapshot))
        self.assertEqual(self.ingest("one", pair)["new"], 2)

    def test_boilerplate_and_new_build_copy_are_not_identities(self):
        pair = self.pair()
        self.assertFalse(description_fingerprints({**pair[0], "listing_kind": "new_build", "plot_number": "12"}, self.job))
        path = self.job / pair[1]["evidence"][0]["file"]
        snapshot = json.loads(path.read_text())
        snapshot["text"] = "About this property A nice home. Read full description " + DESCRIPTION
        path.write_text(json.dumps(snapshot))
        self.assertFalse(description_fingerprints(pair[1], self.job))

    def test_different_unit_never_matches_shared_description(self):
        pair = self.pair()
        # Even identical prose and a reused full description cannot override units.
        from identity_evidence import corroborating_facts
        self.assertFalse(corroborating_facts({**pair[0], "address": "Flat A, 16 Heather Gardens"},
                                           {**pair[1], "address": "Flat B, 16 Heather Gardens"}))


if __name__ == "__main__":
    unittest.main()
