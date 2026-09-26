"""Best-effort property thumbnail discovery; only verified hotlinks are stored."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from PIL import Image

from fetch import public_url


BUDGET_SECONDS = 60
PROPERTY_BUDGET_SECONDS = 20
MAX_WORKERS = 4
UNSUITABLE = re.compile(r"logo|favicon|sprite|floor[\W_]*plan|\bepc\b|energy[\W_]*(?:rating|performance|certificate)|placeholder|coming[\W_]*soon|avatar|(?:^|[/_. -])(?:icon|badge|map)(?:[/_. -]|$)", re.I)


def _request(url, deadline, max_bytes=3_000_000):
    """Fetch a bounded public response, validating each redirect destination."""
    with tempfile.TemporaryDirectory(prefix="dundee-photo-") as temporary:
        path = Path(temporary) / "body"
        current = url
        for _ in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Thumbnail time budget reached")
            public_url(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Thumbnail time budget reached")
            timeout = min(6, remaining)
            response = subprocess.run([
                "curl", "--silent", "--show-error", "--compressed", "--max-time", str(timeout),
                "--connect-timeout", str(min(3, timeout)), "--max-filesize", str(max_bytes),
                "--proto", "=http,https", "--referer", "https://ivyf.net/",
                "--user-agent", "Mozilla/5.0 (compatible; DundeePropertyWatch/1.0)",
                "--output", str(path), "--write-out", "%{json}", current,
            ], capture_output=True, text=True, timeout=timeout + 0.5)
            metadata = json.loads(response.stdout or "{}")
            if response.returncode:
                raise ValueError("Thumbnail request failed")
            status = metadata.get("http_code")
            redirect = metadata.get("redirect_url")
            if status in {301, 302, 303, 307, 308} and redirect:
                current = urljoin(current, redirect)
                continue
            if status != 200:
                raise ValueError("Thumbnail response was not successful")
            body = path.read_bytes()
            if len(body) > max_bytes:
                raise ValueError("Thumbnail response is too large")
            return current, (metadata.get("content_type") or "").split(";", 1)[0].lower(), body
    raise ValueError("Too many thumbnail redirects")


def image_candidates(html, page_url):
    """Prefer listing metadata, then listing-specific gallery/main images."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen = set()

    def add(value, description=""):
        if not isinstance(value, str) or not value.strip():
            return
        url = urljoin(page_url, value.strip())
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return
        if UNSUITABLE.search(url + " " + description) or url in seen:
            return
        seen.add(url)
        candidates.append(url)

    for key in ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"):
        for node in soup.find_all("meta", attrs={"property": key}) + soup.find_all("meta", attrs={"name": key}):
            add(node.get("content"))

    def image_value(value):
        if isinstance(value, str):
            add(value)
        elif isinstance(value, list):
            for item in value:
                image_value(item)
        elif isinstance(value, dict):
            add(value.get("contentUrl") or value.get("url"), str(value.get("caption", "")))

    def structured(value):
        if isinstance(value, list):
            for item in value:
                structured(item)
        elif isinstance(value, dict):
            kind = value.get("@type", "")
            if kind in ("Organization", "RealEstateAgent", "Person", "Brand"):
                return
            for key, item in value.items():
                if key in {"image", "photo", "thumbnailUrl"}:
                    image_value(item)
                elif key not in {"logo", "publisher", "author", "seller", "brand"}:
                    structured(item)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            structured(json.loads(script.get_text()))
        except (TypeError, ValueError):
            continue
    nodes = soup.select("main img, [class*='gallery'] img, [id*='gallery'] img, [class*='property'] img")
    for node in nodes:
        description = " ".join([str(node.get("alt", "")), str(node.get("class", "")), str(node.get("id", ""))])
        try:
            if (node.get("width") and int(node["width"]) < 320) or (node.get("height") and int(node["height"]) < 180):
                continue
        except ValueError:
            pass
        srcset = node.get("data-srcset") or node.get("srcset")
        if srcset:
            add(srcset.split(",")[-1].strip().split()[0], description)
        add(node.get("data-src") or node.get("src"), description)
    return candidates[:12]


def probe_image(url, deadline):
    actual_url, content_type, data = _request(url, deadline, max_bytes=5_000_000)
    if not content_type.startswith("image/") or UNSUITABLE.search(actual_url):
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if image.format not in {"JPEG", "PNG", "WEBP", "AVIF"} or width < 320 or height < 180 or not 0.5 <= width / height <= 3:
                return None
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    return actual_url


def discover_photo(property_data, deadline):
    """Use the original listing first, followed by known matching portal pages."""
    deadline = min(deadline, time.monotonic() + PROPERTY_BUDGET_SECONDS)
    urls = [property_data["url"]] + [link["url"] for link in property_data.get("additional_urls", [])]
    urls = list(dict.fromkeys(urls))[:4]
    for url in urls:
        if time.monotonic() >= deadline:
            break
        # Leave time for portal fallbacks when an original page is unhelpful.
        page_deadline = min(deadline, time.monotonic() + 8)
        try:
            page_url, content_type, body = _request(url, page_deadline)
            if content_type not in {"text/html", "application/xhtml+xml"}:
                continue
            for candidate in image_candidates(body, page_url)[:3]:
                if time.monotonic() >= page_deadline:
                    break
                try:
                    image_url = probe_image(candidate, page_deadline)
                    if image_url:
                        return image_url, page_url
                except Exception:
                    continue
        except Exception:
            continue
    return None, None


def enrich_photos(db, day=None):
    """Cache successful and unsuccessful attempts; SQLite stays on this thread."""
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS photos (
            property_id TEXT PRIMARY KEY REFERENCES properties(id),
            image_url TEXT, page_url TEXT, checked_at TEXT NOT NULL)""")
    query = """SELECT p.id,p.data FROM properties p
               WHERE NOT EXISTS (SELECT 1 FROM photos f WHERE f.property_id=p.id)
               AND EXISTS (SELECT 1 FROM reports r WHERE r.property_id=p.id"""
    values = ()
    if day is not None:
        query += " AND r.day=?"
        values = (day,)
    query += ") ORDER BY p.first_seen DESC"
    pending_rows = iter(db.execute(query, values).fetchall())
    deadline = time.monotonic() + BUDGET_SECONDS
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    pending = {}
    attempted = added = 0

    def submit_next():
        if time.monotonic() >= deadline:
            return
        row = next(pending_rows, None)
        if row is not None:
            pending[pool.submit(discover_photo, json.loads(row[1]), deadline)] = row[0]

    try:
        for _ in range(MAX_WORKERS):
            submit_next()
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                property_id = pending.pop(future)
                try:
                    image_url, page_url = future.result()
                except Exception:
                    image_url = page_url = None
                with db:
                    db.execute("INSERT OR IGNORE INTO photos VALUES (?,?,?,?)", (property_id, image_url, page_url, datetime.now(timezone.utc).isoformat()))
                attempted += 1
                added += bool(image_url)
                submit_next()
        # Every submitted task begins immediately (there are at most MAX_WORKERS).
        # Cache timed-out attempts too, without delaying the daily report.
        with db:
            for future, property_id in pending.items():
                future.cancel()
                db.execute("INSERT OR IGNORE INTO photos VALUES (?,NULL,NULL,?)", (property_id, datetime.now(timezone.utc).isoformat()))
                attempted += 1
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {"attempted": attempted, "added": added}
