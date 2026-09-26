"""Offline thumbnail selection and successful/unsuccessful cache checks."""
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from photos import cache_photos, discover_photo, download_thumbnail, enrich_photos, image_candidates, probe_image
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

    def test_download_produces_small_metadata_free_jpeg_and_rejects_bad_content(self):
        original = BytesIO()
        metadata = Image.Exif()
        metadata[270] = 'Private camera description'
        Image.new('RGB', (1600, 1000), 'green').save(original, format='JPEG', exif=metadata)
        url = 'https://images.example/house.jpg'
        with patch('photos._request', return_value=(url, 'image/jpeg', original.getvalue())):
            jpeg = download_thumbnail(url, float('inf'))
        with Image.open(BytesIO(jpeg)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (960, 600))
            self.assertFalse(image.getexif())
            image.verify()
        self.assertLess(len(jpeg), len(original.getvalue()))
        for content_type, body in [('text/html', original.getvalue()), ('image/jpeg', b'not a photo')]:
            with self.subTest(content_type=content_type), patch('photos._request', return_value=(url, content_type, body)):
                with self.assertRaises((OSError, ValueError)):
                    download_thumbnail(url, float('inf'))

    def test_cached_photos_survive_source_failure_and_failed_downloads_retry_later(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory))
            with db:
                for index in range(3):
                    property_id = str(index)
                    db.execute('INSERT INTO properties VALUES (?,?,?,?)', (property_id, '{}', '2026-01-01', '2026-01-01'))
                    db.execute('INSERT INTO reports VALUES (?,?,?)', ('2026-01-01', property_id, '{}'))
                    # Two homes sharing one image should cause only one download.
                    url = 'https://images.example/' + ('good.jpg' if index < 2 else 'failed.jpg')
                    db.execute('INSERT INTO photos VALUES (?,?,?,?)', (property_id, url, 'https://agent.example/', '2026-01-01'))
            def download(url, deadline):
                if url.endswith('failed.jpg'):
                    raise OSError('Source temporarily unavailable')
                return b'cached JPEG bytes'
            with patch('photos.download_thumbnail', side_effect=download) as downloads:
                self.assertEqual(cache_photos(db), {'attempted': 2, 'cached': 1})
                self.assertEqual(cache_photos(db), {'attempted': 0, 'cached': 0})
                self.assertEqual(downloads.call_count, 2)
            with db:
                db.execute("UPDATE photo_cache SET checked_at='2020-01-01'")
            with patch('photos.download_thumbnail', return_value=b'recovered image') as downloads:
                self.assertEqual(cache_photos(db), {'attempted': 1, 'cached': 1})
                self.assertEqual(downloads.call_args.args[0], 'https://images.example/failed.jpg')
            self.assertEqual(db.execute("SELECT jpeg FROM photo_cache WHERE image_url LIKE '%good.jpg'").fetchone()[0], b'cached JPEG bytes')
            db.close()

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
