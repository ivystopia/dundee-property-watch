"""Private, cached location annotations; never rewrite historical listing facts."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from fetch import public_url
from model import canonical_url


ROUTER = "https://valhalla1.openstreetmap.de/route"
AREAS = ("Baxter Park", "Dykes of Gray", "Western Gateway", "Panmurefield", "City Quay", "Stobswell", "Hilltown", "Fairmuir", "Coldside",
         "Dudhope", "Blackness", "West End", "Lochee", "Logie", "Balgay", "Menzieshill", "Charleston",
         "Ninewells", "Ardler", "Dryburgh", "Downfield", "Kirkton", "St Mary's", "Brackens", "Claverhouse",
         "Fintry", "Whitfield", "Mill of Mains", "Linlathen", "Craigiebank", "Craigiebarns", "Douglas",
         "Barnhill", "Forthill", "Balgillo", "Ballumbie", "Maryfield", "Woodside", "Pitkerro", "Riverside", "Strathmartine", "Broughty Ferry")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def valid_point(lat, lon):
    return (type(lat) in {int, float} and type(lon) in {int, float}
            and math.isfinite(lat) and math.isfinite(lon) and 56.43 < lat < 56.53 and -3.16 < lon < -2.76)


def extract_map(html):
    """Read the individual property map, never related cards or office coordinates."""
    soup = BeautifulSoup(html, "html.parser")
    description = ""
    point = None
    match = re.search(r"window\.__PAGE_MODEL\s*=\s*", html)
    if match:
        try:
            model = json.JSONDecoder().raw_decode(html[match.end():])[0]
            pool = json.loads(model["data"])
            prop = pool[pool[0]["propertyData"]]
            loc = pool[prop["location"]]
            lat, lon = pool[loc["latitude"]], pool[loc["longitude"]]
            if valid_point(lat, lon):
                point = (lat, lon, "Rightmove individual listing map")
            text = pool[prop["text"]]
            description = BeautifulSoup(pool[text["description"]], "html.parser").get_text(" ", strip=True)
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    for selector in ("#property-map[data-lat][data-lng]", "#map[data-address][data-lat][data-lng]"):
        element = soup.select_one(selector)
        if element:
            try:
                lat, lon = float(element["data-lat"]), float(element["data-lng"])
                if valid_point(lat, lon):
                    point = (lat, lon, str(element)[:350])
                    break
            except ValueError:
                pass
    if not point:
        schema_points = set()

        def visit(value):
            if isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, dict):
                kind = str(value.get("@type", ""))
                if any(name in kind for name in ("LocalBusiness", "Organization", "RealEstateAgent")):
                    return
                geo = value.get("geo")
                if isinstance(geo, dict):
                    try:
                        lat, lon = float(geo["latitude"]), float(geo["longitude"])
                        if valid_point(lat, lon):
                            schema_points.add((lat, lon))
                    except (KeyError, TypeError, ValueError):
                        pass
                for item in value.values():
                    visit(item)

        for element in soup.select('script[type="application/ld+json"]'):
            try:
                visit(json.loads(element.get_text()))
            except (TypeError, ValueError):
                pass
        if len(schema_points) == 1:
            lat, lon = schema_points.pop()
            point = (lat, lon, "Individual listing's structured property GeoCoordinates")
    # Lindsays' individual map uses a literal lat/lng pair; other sites need
    # an unambiguous single pair. Related properties and office maps disqualify it.
    if not point:
        pairs = set()
        for element in soup.select('.property-card, footer, header, script[type="application/ld+json"]'):
            if element.name != "script" or re.search(r'LocalBusiness|Organization|RealEstateAgent', element.get_text()):
                element.decompose()
        html = str(soup)
        pattern = r'\blat(?:itude)?["\s]*:\s*["\s]*(56\.\d+)["\s]*,\s*["\s]*l(?:ng|on|ongitude)["\s]*:\s*["\s]*(-[23]\.\d+)'
        for match in re.finditer(pattern, html, re.I):
            lat, lon = float(match[1]), float(match[2])
            if valid_point(lat, lon):
                pairs.add((lat, lon))
        if len(pairs) == 1:
            lat, lon = pairs.pop()
            point = (lat, lon, "Individual listing's unique map coordinate pair")
    # Prefer the property description to menus, office footers and related cards.
    if not description:
        for selector in (".property-description", "#description", ".description", "[itemprop=description]"):
            element = soup.select_one(selector)
            if element:
                description = element.get_text(" ", strip=True)
                break
    area, quote = None, None
    if description:
        for name in AREAS:
            match = re.search(r"(?:located|situated|positioned|nestled|set|within|in)\b[^.!?]{0,100}\b(" + re.escape(name) + r")\b", description, re.I)
            if match:
                # Amenity proximity is not proof of the property's neighbourhood.
                if re.search(r"(?:close to|near to|minutes|access|links to|walking|driving|reach|bus|travel)\b", match[0], re.I):
                    continue
                area, quote = name, match[0]
                break
    return point, area, quote


def fetch_listing(url, directory, timeout=10):
    """Save public HTML privately, validating every redirect before following it."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(url.encode()).hexdigest()
    current = url
    body = directory / (key + ".html")
    deadline = time.monotonic() + timeout
    for _ in range(5):
        remaining = deadline - time.monotonic()
        if remaining < 1:
            raise ValueError("Listing-map request exceeded its budget")
        public_url(current)
        result = subprocess.run(["curl", "-sS", "--compressed", "--max-time", str(remaining),
                                 "--max-filesize", "5000000", "-A", "Mozilla/5.0 (compatible; DundeePropertyWatch/1.0)",
                                 "-o", str(body), "-w", "%{json}", current], capture_output=True, text=True, timeout=remaining + 1)
        info = json.loads(result.stdout or "{}")
        if result.returncode:
            raise ValueError("Listing map retrieval failed")
        if info.get("redirect_url") and info.get("http_code") in {301, 302, 303, 307, 308}:
            current = info["redirect_url"]
            continue
        if info.get("http_code") != 200:
            raise ValueError("Listing map HTTP " + str(info.get("http_code")))
        html = body.read_text(errors="replace")
        (directory / (key + ".json")).write_text(json.dumps({"url": url, "final_url": current, "retrieved_at": stamp(), "status": 200, "file": body.name}))
        return html
    raise ValueError("Too many listing redirects")


def origin_config(state):
    path = Path(state) / "locations" / "origin.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if not valid_point(data.get("latitude"), data.get("longitude")) or not data.get("source"):
        raise ValueError("The walking origin needs a verified Dundee map point")
    return data


def route_key(origin, destination):
    value = [origin["latitude"], origin["longitude"], destination["latitude"], destination["longitude"], "pedestrian", 5.0, ROUTER]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def request_route(origin, destination, timeout=15):
    payload = {"locations": [{"lat": origin["latitude"], "lon": origin["longitude"]},
                             {"lat": destination["latitude"], "lon": destination["longitude"]}],
               "costing": "pedestrian", "units": "miles", "costing_options": {"pedestrian": {"walking_speed": 5.0}}}
    request = Request(ROUTER, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "User-Agent": "DundeePropertyWatch/1.0",
        "X-Client-Id": "ivystopia.github.io/dundee-property-watch"})
    with urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    trip = result["trip"]
    summary = trip["summary"]
    miles, minutes = summary["length"], summary["time"] / 60
    if trip.get("status") != 0 or trip.get("units") != "miles" or not (0 <= miles < 50 and 0 <= minutes < 1500):
        raise ValueError("Invalid pedestrian route response")
    if summary.get("has_ferry"):
        raise ValueError("A ferry itinerary is not a walking route")
    return {"distance_miles": miles, "duration_minutes": minutes, "route_source": ROUTER,
            "route_checked_at": stamp(), "origin_key": route_key(origin, destination),
            "route_request": payload, "route_summary": summary, "route_locations": trip.get("locations", [])}


def validate_annotation(data):
    if data.get("area") and (not data.get("area_source") or not isinstance(data["area"], str)):
        raise ValueError("Area needs a source")
    if data.get("latitude") is not None:
        if not valid_point(data.get("latitude"), data.get("longitude")) or not data.get("coordinate_source"):
            raise ValueError("Coordinates need a verified source within Dundee")
        if data.get("precision") not in {"listing_map", "building", "street", "postcode"}:
            raise ValueError("Location precision must be explicit")
    if "distance_miles" in data or "duration_minutes" in data:
        if not all(type(data.get(k)) in {int, float} and math.isfinite(data[k]) and data[k] >= 0
                   for k in ("distance_miles", "duration_minutes")) or not data.get("origin_key"):
            raise ValueError("Walking figures need a valid paired route")


def save_annotation(db, property_id, data, *, commit=True):
    validate_annotation(data)
    if not db.execute("SELECT 1 FROM properties WHERE id=?", (property_id,)).fetchone():
        raise ValueError("Unknown archived property")
    previous = db.execute("SELECT data FROM property_locations WHERE property_id=?", (property_id,)).fetchone()
    if not previous or json.loads(previous[0]) != data:
        db.execute("INSERT OR REPLACE INTO presentation_state VALUES ('locations_dirty','1')")
    db.execute("INSERT INTO property_locations VALUES (?,?,?) ON CONFLICT(property_id) DO UPDATE SET data=excluded.data,checked_at=excluded.checked_at",
               (property_id, json.dumps(data), stamp()))
    if commit:
        db.commit()


def pending_publication(db):
    return bool(db.execute("SELECT 1 FROM presentation_state WHERE key IN ('locations_dirty','photos_dirty')").fetchone())


def import_annotations(db, records):
    for record in records:
        validate_annotation(record["location"])
        if not db.execute("SELECT 1 FROM properties WHERE id=?", (record["property_id"],)).fetchone():
            raise ValueError("Unknown archived property")
    with db:
        for record in records:
            save_annotation(db, record["property_id"], record["location"], commit=False)
            data = record["location"]
            if data.get("origin_key") and data.get("route_request"):
                route = {key: value for key, value in data.items() if key in {
                    "origin_key", "distance_miles", "duration_minutes", "route_source", "route_checked_at",
                    "route_request", "route_summary", "route_locations"}}
                db.execute("INSERT OR IGNORE INTO walking_routes VALUES (?,?)", (data["origin_key"], json.dumps(route)))


def apply_hints(db, result, job):
    """Accept source-backed area hints separately from the listing schema/history."""
    for hint in result.get("location_hints", []):
        try:
            path = (job / hint["file"]).resolve()
            if not path.is_relative_to((job / "evidence").resolve()):
                continue
            snapshot = json.loads(path.read_text())
            quote = " ".join(hint["quote"].split())
            text = " ".join(snapshot["text"].split())
            if snapshot.get("status") != 200 or snapshot.get("error") or canonical_url(snapshot["url"]) != canonical_url(hint["url"]):
                continue
            if len(quote) < 15 or quote not in text or hint["area"].casefold() not in quote.casefold():
                continue
            row = db.execute("SELECT p.id,l.data FROM properties p JOIN aliases a ON a.property_id=p.id LEFT JOIN property_locations l ON l.property_id=p.id WHERE a.alias=?", ("url:" + canonical_url(hint["url"]),)).fetchone()
            if row:
                data = json.loads(row[1]) if row[1] else {}
                data.update(area=hint["area"], area_source=hint["url"], area_quote=quote, reviewed=True)
                save_annotation(db, row[0], data)
        except (KeyError, TypeError, ValueError, OSError):
            continue


def enrich_locations(db, state=None, *, budget=120):
    """Bounded cached enrichment; failures never reject a home or stop publication."""
    state = Path(state) if state is not None else Path(db.execute("PRAGMA database_list").fetchone()[2]).parent
    try:
        origin = origin_config(state)
    except (OSError, ValueError):
        return {"enabled": False, "error": "Walking origin configuration needs review"}
    if not origin:
        return {"enabled": False}
    deadline, last_request = time.monotonic() + budget, 0.0
    counts = {"updated": 0, "pending": 0}
    rows = db.execute("SELECT p.id,p.data,l.data AS location,l.checked_at FROM properties p JOIN reports r ON r.property_id=p.id LEFT JOIN property_locations l ON l.property_id=p.id ORDER BY r.day DESC").fetchall()
    for row in rows:
        if time.monotonic() >= deadline - 2:
            break
        data = json.loads(row["location"]) if row["location"] else {}
        current_route = data.get("latitude") is not None and data.get("origin_key") == route_key(origin, data)
        if current_route and data.get("area"):
            continue
        if row["checked_at"] and data.get("error") and not data.get("reviewed") and (datetime.now(timezone.utc) - datetime.fromisoformat(row["checked_at"])).total_seconds() < 86400:
            continue
        try:
            if not current_route:
                for field in ("origin_key", "distance_miles", "duration_minutes", "route_source", "route_checked_at"):
                    data.pop(field, None)
            if data.get("latitude") is None or not data.get("area"):
                prop = json.loads(row["data"])
                urls = [prop["url"]] + [v["url"] for v in prop.get("additional_urls", []) if "rightmove.co.uk/properties/" in v["url"]]
                for url in urls[:2]:
                    remaining = deadline - time.monotonic()
                    if remaining < 4:
                        break
                    try:
                        html = fetch_listing(url, state / "locations" / "evidence", timeout=min(10, max(1, int(remaining - 2))))
                        point, area, quote = extract_map(html)
                        if area:
                            data.update(area=area, area_source=url, area_quote=quote)
                        if point and data.get("latitude") is None:
                            data.update(latitude=point[0], longitude=point[1], coordinate_source=url, coordinate_quote=point[2], precision="listing_map")
                        if data.get("latitude") is not None and data.get("area"):
                            break
                    except (ValueError, OSError, subprocess.SubprocessError):
                        continue
            if data.get("latitude") is not None:
                key = route_key(origin, data)
                cached = db.execute("SELECT data FROM walking_routes WHERE key=?", (key,)).fetchone()
                if cached:
                    route = json.loads(cached[0])
                else:
                    time.sleep(max(0, 1.1 - (time.monotonic() - last_request)))
                    remaining = deadline - time.monotonic()
                    if remaining < 3:
                        break
                    last_request = time.monotonic()
                    route = request_route(origin, data, timeout=min(15, remaining - 1))
                    with db:
                        db.execute("INSERT INTO walking_routes VALUES (?,?)", (key, json.dumps(route)))
                data.update(route)
                data.pop("error", None)
                counts["updated"] += 1
            else:
                data["error"] = "Individual listing map not verified"
                counts["pending"] += 1
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            data["error"] = "Walking estimate pending: " + type(exc).__name__
            counts["pending"] += 1
        save_annotation(db, row["id"], data)
    return counts


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["import", "enrich", "status"])
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/dundee-property-watch")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--budget", type=int, default=120)
    args = parser.parse_args()
    from native_job import locked_state, active_lease
    with locked_state(args.state) as db:
        if active_lease(db):
            raise ValueError("Another native run is active")
        if args.action == "import":
            records = json.loads(args.file.read_text())
            # A bad record must not leave a partially applied backfill.
            import_annotations(db, records)
            print(json.dumps({"imported": len(records)}))
        elif args.action == "enrich":
            print(json.dumps(enrich_locations(db, args.state, budget=args.budget)))
        else:
            rows = [json.loads(row[0]) for row in db.execute("SELECT data FROM property_locations")]
            print(json.dumps({"annotated": len(rows), "areas": sum(bool(r.get('area')) for r in rows), "walks": sum('distance_miles' in r for r in rows)}))
