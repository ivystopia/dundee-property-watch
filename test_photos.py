"""Offline thumbnail selection and successful/unsuccessful cache checks."""
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from photos import discover_photo, enrich_photos, image_candidates, probe_image
from store import connect


class PhotoTests(unittest.TestCase):
    def test_parser_prefers_property_images_and_rejects_branding_and_floorplans(self):
        html = '''<meta property="og:image" content="/brand-logo.png">
        <meta name="twitter:image" content="https://images.example/house-front.jpg">
        <script type="application/ld+json">{"@type":"Organization","image":"/office.jpg","logo":"/opaque.png"}</script>
        <script type="application/ld+json">{"@type":"Residence","image":{"contentUrl":"/garden.jpg"}}</script>
        <main><img src="/floor-plan.png"><img src="/small.jpg" width="10" height="7">
        <img src="/opaque-epc.png" alt="EPC"><img src="/kitchen.jpg" width="800" height="600"></main>'''
        self.assertEqual(image_candidates(html, "https://agent.example/property/123"), [
            "https://images.example/house-front.jpg", "https://agent.example/garden.jpg", "https://agent.example/kitchen.jpg"])

    def test_probe_requires_real_photo_dimensions_and_image_content_type(self):
        def picture(size):
            result = BytesIO()
            Image.new("RGB", size, "green").save(result, format="JPEG")
            return result.getvalue()
        url = "https://images.example/property.jpg"
        for content_type, body, expected in [("image/jpeg", picture((1024, 683)), url),
                                             ("image/jpeg", picture((10, 7)), None),
                                             ("text/html", picture((1024, 683)), None),
                                             ("image/jpeg", b"not really an image", None)]:
            with self.subTest(content_type=content_type, size=len(body)), patch("photos._request", return_value=(url, content_type, body)):
                self.assertEqual(probe_image(url, 100), expected)

    def test_original_logo_falls_back_to_matching_portal_photo(self):
        original = "https://builder.example/plot/49"
        portal = "https://portal.example/details/123"
        photo = "https://images.example/plot49.jpg"
        def request(url, deadline):
            content = '<meta property="og:image" content="/logo.png">' if url == original else f'<meta property="og:image" content="{photo}">'
            return url, "text/html", content.encode()
        with patch("photos._request", side_effect=request) as requests, patch("photos.probe_image", return_value=photo):
            self.assertEqual(discover_photo({"url": original, "additional_urls": [{"url": portal}]}, float("inf")), (photo, portal))
        self.assertEqual([call.args[0] for call in requests.call_args_list], [original, portal])

    def test_success_and_failure_are_cached_without_cross_thread_sqlite_access(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory))
            with db:
                for index, day in enumerate(["2026-09-16", "2026-09-16", "2026-09-15"]):
                    data = json.dumps({"url": f"https://agent.example/property/{index}"})
                    db.execute("INSERT INTO properties VALUES (?,?,?,?)", (str(index), data, day, day))
                    db.execute("INSERT INTO reports VALUES (?,?,?)", (day, str(index), data))
            def lookup(data, deadline):
                return ("https://images.example/photo.jpg", data["url"]) if data["url"].endswith("/0") else (None, None)
            with patch("photos.discover_photo", side_effect=lookup) as lookups:
                self.assertEqual(enrich_photos(db, "2026-09-16"), {"attempted": 2, "added": 1})
                self.assertEqual(enrich_photos(db, "2026-09-16"), {"attempted": 0, "added": 0})
                self.assertEqual(lookups.call_count, 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 2)
            self.assertIsNone(db.execute("SELECT image_url FROM photos WHERE property_id='1'").fetchone()[0])
            self.assertIsNone(db.execute("SELECT image_url FROM photos WHERE property_id='2'").fetchone())
            db.close()


if __name__ == "__main__":
    unittest.main()
