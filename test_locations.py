"""Location annotations must not corrupt history or block a valid daily report."""
import json
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch

import test_watch
import locations
import watch
from render import render


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_watch.WatchTests()
        self.fixture.setUp()
        self.db = self.fixture.db
        self.state = self.fixture.root / "state"
        self.fixture.ingest("one", self.fixture.result())
        self.pid = self.db.execute("SELECT id FROM properties").fetchone()[0]
        self.original = self.db.execute("SELECT data FROM reports").fetchone()[0]
        self.origin = {"latitude": 56.49, "longitude": -3.02, "source": "https://example.org/origin"}
        (self.state / "locations").mkdir()
        self.origin_path = self.state / "locations/origin.json"
        self.origin_path.write_text(json.dumps(self.origin))
        self.annotation = {"area": "Hilltown", "area_source": "https://example.org/property",
                           "latitude": 56.47, "longitude": -2.97, "coordinate_source": "https://example.org/property",
                           "precision": "listing_map", "reviewed": True}

    def tearDown(self):
        self.fixture.tearDown()

    def route(self, origin, destination, **kwargs):
        return {"distance_miles": 1.24, "duration_minutes": 24.6, "origin_key": locations.route_key(origin, destination)}

    def test_reviewed_annotation_is_idempotent_and_preserves_history(self):
        locations.save_annotation(self.db, self.pid, self.annotation)
        self.db.execute("DELETE FROM presentation_state")
        self.db.commit()
        locations.save_annotation(self.db, self.pid, self.annotation)
        self.assertFalse(locations.pending_publication(self.db))
        self.assertEqual(self.db.execute("SELECT data FROM reports").fetchone()[0], self.original)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1)

    def test_routes_cache_and_origin_changes_invalidate(self):
        locations.save_annotation(self.db, self.pid, self.annotation)
        with patch("locations.request_route", side_effect=self.route) as request:
            locations.enrich_locations(self.db, self.state)
            locations.enrich_locations(self.db, self.state)
            self.assertEqual(request.call_count, 1)
            changed = dict(self.origin, latitude=56.477)
            self.origin_path.write_text(json.dumps(changed))
            locations.enrich_locations(self.db, self.state)
            self.assertEqual(request.call_count, 2)

    def test_route_failure_keeps_area_and_existing_report(self):
        locations.save_annotation(self.db, self.pid, self.annotation)
        with patch("locations.request_route", side_effect=OSError("unavailable")):
            result = locations.enrich_locations(self.db, self.state)
        self.assertEqual(result["pending"], 1)
        page = render(self.db, self.fixture.root / "site").read_text()
        self.assertIn("Hilltown", page)
        self.assertIn("Walking distance unavailable", page)
        self.assertEqual(self.db.execute("SELECT data FROM reports").fetchone()[0], self.original)

    def test_render_rounding_street_precision_and_private_sources(self):
        data = dict(self.annotation, **self.route(self.origin, self.annotation), precision="street",
                    area="Baxter Park & Stobswell", private_proof="PRIVATE-PROOF-SENTINEL")
        locations.save_annotation(self.db, self.pid, data)
        page = render(self.db, self.fixture.root / "site").read_text()
        self.assertIn("Baxter Park &amp; Stobswell", page)
        self.assertIn("Approx. 1.2 miles · 25 min walk from Tip Top", page)
        self.assertIn("Street-level estimate", page)
        self.assertNotIn("PRIVATE-PROOF-SENTINEL", page)
        self.assertNotIn(self.annotation["coordinate_source"], page)

    def test_missing_metadata_still_renders_card(self):
        page = render(self.db, self.fixture.root / "site").read_text()
        self.assertIn("Area to confirm", page)
        self.assertIn("Walking distance unavailable", page)
        self.assertIn(self.fixture.p["address"], page)

    def test_failed_publication_keeps_annotation_pending(self):
        locations.save_annotation(self.db, self.pid, self.annotation)
        with patch("watch.enrich_locations", return_value={}), patch("watch.enrich_photos"), patch("watch.cache_photos", return_value={}), patch("watch.render"), patch("watch.publish_site", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                watch.publish(self.db)
        self.assertTrue(locations.pending_publication(self.db))
        with patch("watch.enrich_locations", return_value={}), patch("watch.enrich_photos"), patch("watch.cache_photos", return_value={}), patch("watch.render"), patch("watch.publish_site", return_value={"url": "https://example.org/"}):
            watch.publish(self.db)
        self.assertFalse(locations.pending_publication(self.db))

    def test_office_and_related_map_points_are_not_property_locations(self):
        html = '<script type="application/ld+json">{"@type":"RealEstateAgent","geo":{"latitude":56.46,"longitude":-2.97}}</script><div class="property-card" data-lat="56.48" data-lng="-2.99"></div>'
        self.assertIsNone(locations.extract_map(html)[0])
        html += '<div id="map" data-address="12 Correct Street" data-lat="56.47" data-lng="-2.98"></div>'
        self.assertEqual(locations.extract_map(html)[0][:2], (56.47, -2.98))

    def test_structured_property_point_excludes_nested_agent(self):
        data = {"@graph": [{"@type": "House", "geo": {"latitude": 56.48, "longitude": -2.99}},
                           {"@type": "RealEstateAgent", "geo": {"latitude": 56.46, "longitude": -2.97}}]}
        self.assertEqual(locations.extract_map('<script type="application/ld+json">'+json.dumps(data)+'</script>')[0][:2], (56.48, -2.99))

    def test_invalid_annotation_is_rejected_before_history_changes(self):
        for change in [{"latitude": float('nan')}, {"longitude": 10}, {"area_source": ""}, {"distance_miles": 1}]:
            with self.assertRaises(ValueError):
                locations.save_annotation(self.db, self.pid, dict(self.annotation, **change))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM property_locations").fetchone()[0], 0)

    def test_area_hint_requires_matching_listing_and_saved_quote(self):
        snapshot = self.fixture.job / 'evidence/area.json'
        snapshot.write_text(json.dumps({"url": self.fixture.p['url'], "status": 200, "text": "Located in the popular Hilltown area of Dundee."}))
        hint = {"url": self.fixture.p['url'], "file": 'evidence/area.json', "area": 'Hilltown', "quote": "Located in the popular Hilltown area of Dundee."}
        locations.apply_hints(self.db, {"location_hints": [hint]}, self.fixture.job)
        self.assertEqual(json.loads(self.db.execute("SELECT data FROM property_locations").fetchone()[0])['area'], 'Hilltown')
        hint.update(area='Baxter Park', quote='A fabricated Baxter Park description.')
        locations.apply_hints(self.db, {"location_hints": [hint]}, self.fixture.job)
        self.assertEqual(json.loads(self.db.execute("SELECT data FROM property_locations").fetchone()[0])['area'], 'Hilltown')

    def test_bulk_import_validates_all_rows_before_writing(self):
        records = [{"property_id": self.pid, "location": self.annotation},
                   {"property_id": "missing", "location": self.annotation}]
        with self.assertRaises(ValueError):
            locations.import_annotations(self.db, records)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM property_locations").fetchone()[0], 0)
        self.assertFalse(locations.pending_publication(self.db))

    def test_provider_request_uses_pedestrian_costing_and_miles(self):
        response = {"trip": {"status": 0, "units": "miles", "summary": {"length": 1.25, "time": 1500, "has_ferry": False}}}
        with patch('locations.urlopen', return_value=BytesIO(json.dumps(response).encode())) as request:
            route = locations.request_route(self.origin, self.annotation)
        payload = json.loads(request.call_args[0][0].data)
        self.assertEqual(payload['costing'], 'pedestrian')
        self.assertEqual(payload['units'], 'miles')
        self.assertEqual(route['duration_minutes'], 25)
        self.assertEqual(route['distance_miles'], 1.25)
        response['trip']['summary']['has_ferry'] = True
        with patch('locations.urlopen', return_value=BytesIO(json.dumps(response).encode())):
            with self.assertRaises(ValueError):
                locations.request_route(self.origin, self.annotation)


if __name__ == '__main__':
    unittest.main()
