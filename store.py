"""Private SQLite history; public reports are an explicit, limited projection."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from model import MAX_PRICE_GBP, canonical_url, identities, merge_property_data, normalize, property_id, same_addressed_home, validate_candidate


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(state / "history.sqlite3")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, started TEXT NOT NULL, finished TEXT,
            day TEXT NOT NULL, status TEXT NOT NULL, error TEXT, published TEXT);
        CREATE TABLE IF NOT EXISTS native_leases (
            run_id TEXT PRIMARY KEY REFERENCES runs(id), job TEXT NOT NULL,
            deadline REAL NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_checks (
            run_id TEXT REFERENCES runs(id), source_id TEXT, status TEXT, detail TEXT, urls TEXT,
            PRIMARY KEY(run_id, source_id));
        CREATE TABLE IF NOT EXISTS source_price_coverage (
            source_id TEXT PRIMARY KEY, max_price_gbp INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS properties (
            id TEXT PRIMARY KEY, data TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS aliases (
            alias TEXT PRIMARY KEY, property_id TEXT NOT NULL REFERENCES properties(id));
        CREATE TABLE IF NOT EXISTS reports (
            day TEXT, property_id TEXT REFERENCES properties(id), data TEXT NOT NULL,
            PRIMARY KEY(day, property_id));
        CREATE TABLE IF NOT EXISTS photos (
            property_id TEXT PRIMARY KEY REFERENCES properties(id), image_url TEXT,
            page_url TEXT, checked_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS photo_cache (
            image_url TEXT PRIMARY KEY, jpeg BLOB, checked_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS legacy (
            id TEXT PRIMARY KEY, data TEXT NOT NULL, matched_property_id TEXT REFERENCES properties(id));
        CREATE TABLE IF NOT EXISTS imports (sha256 TEXT PRIMARY KEY, imported TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inventory (
            url TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rejected (
            run_id TEXT REFERENCES runs(id), address TEXT, reason TEXT, data TEXT);
    """)
    return db


def import_export(db, path):
    raw = path.read_bytes()
    digest = hashlib.sha256(b"baseline-import-v3\0" + raw).hexdigest()
    if db.execute("SELECT 1 FROM imports WHERE sha256=?", (digest,)).fetchone():
        return 0
    conversation = json.loads(raw)[0]
    nodes = conversation["mapping"]
    chain = []
    current = conversation["current_node"]
    while current:
        node = nodes[current]
        chain.append(node)
        current = node.get("parent")
    chain.reverse()
    prompt = None
    last_save = 0
    for index, node in enumerate(chain):
        message = node.get("message")
        if not message or message["author"].get("name") != "de1d73e.update":
            continue
        try:
            payload = json.loads("".join(p for p in message["content"].get("parts", []) if isinstance(p, str)))
            if payload.get("status") == "SUCCESS" and "PRIOR-REPORT BASELINE" in payload["jawbone"]["prompt"]:
                prompt = payload["jawbone"]["prompt"]
                last_save = index
        except (ValueError, KeyError):
            pass
    if not prompt:
        raise ValueError("No saved prior-report baseline found in export")
    baseline = prompt.split("PRIOR-REPORT BASELINE", 1)[1].split("OUTPUT", 1)[0]
    records = []
    for address, description in re.findall(r"(?:^|; |included: |configuration:\n)([^;\n]+?) \(([^)]+)\)", baseline, re.M):
        bed = re.search(r"\b([23])(?:\b|-bed)", description)
        price = re.search(r"(?:GBP\s*|,\s*)(\d{5,6})\b", description)
        if bed and price:
            address = address.removeprefix("The report on 15 September 2026 additionally included: ")
            records.append({"address": address.strip(), "bedrooms": int(bed[1]), "price_gbp": int(price[1]),
                            "description": description, "origin": "previous ChatGPT report"})
    # The final saved task predates the same day's manual findings. Include those too.
    for node in chain[last_save + 1:]:
        message = node.get("message")
        if not message or message["author"]["role"] != "assistant" or message.get("channel") != "final":
            continue
        text = "\n".join(p for p in message["content"].get("parts", []) if isinstance(p, str))
        for address in re.findall(r"\*\*did not count (.+?)\*\*", text):
            records.append({"address": address, "bedrooms": None, "price_gbp": None,
                            "description": "Already inspected and explicitly excluded as an older listing / delayed portal syndication, not a new home.",
                            "origin": "ChatGPT manual catch-up exclusion"})
        for line in text.splitlines():
            match = re.match(r"- \*\*(.+?) - (?:GBP |£)([\d,]+).*?\b([23])-bed", line)
            if match:
                records.append({"address": match[1], "price_gbp": int(match[2].replace(",", "")),
                                "bedrooms": int(match[3]), "description": "Previously shown during the manual catch-up.",
                                "origin": "ChatGPT manual report on 16 September 2026"})
    if len(records) < 39:
        raise ValueError(f"Incomplete baseline import: only {len(records)} entries")
    count = 0
    with db:
        # Repair the older importer's accidentally retained sentence prefix in place.
        for old in db.execute("SELECT id,data FROM legacy").fetchall():
            record = json.loads(old["data"])
            fixed = record["address"].removeprefix("The report on 15 September 2026 additionally included: ")
            if fixed != record["address"]:
                record["address"] = fixed
                db.execute("UPDATE legacy SET data=? WHERE id=?", (json.dumps(record), old["id"]))
        for record in records:
            # Postcodes/area suffix variants are kept as hints, never blindly auto-matched.
            key = hashlib.sha256(f"{normalize(record['address'])}:{record['bedrooms']}:{record['price_gbp']}".encode()).hexdigest()[:16]
            if db.execute("SELECT 1 FROM legacy WHERE json_extract(data,'$.address')=? AND json_extract(data,'$.bedrooms')=? AND json_extract(data,'$.price_gbp')=?", (record["address"], record["bedrooms"], record["price_gbp"])).fetchone():
                continue
            cursor = db.execute("INSERT OR IGNORE INTO legacy(id,data) VALUES (?,?)", (key, json.dumps(record)))
            count += cursor.rowcount
        db.execute("INSERT INTO imports VALUES (?,?)", (digest, now()))
    return count


def context(db, sources):
    previous = [{"id": row["id"], "first_seen": row["first_seen"], **json.loads(row["data"])}
                for row in db.execute("SELECT * FROM properties")]
    legacy = [{"id": row["id"], "matched_property_id": row["matched_property_id"], **json.loads(row["data"])}
              for row in db.execute("SELECT * FROM legacy")]
    cutoffs = {}
    previous_checks = {}
    price_expansions = {}
    for source in sources:
        scope = db.execute("SELECT max_price_gbp FROM source_price_coverage WHERE source_id=?", (source["id"],)).fetchone()
        # Runs before the September 2026 budget increase used a £260,000 cap.
        previous_cap = scope[0] if scope else 260000
        if previous_cap < MAX_PRICE_GBP:
            price_expansions[source["id"]] = {"min_price_gbp": previous_cap + 1, "max_price_gbp": MAX_PRICE_GBP}
        row = db.execute("""SELECT MAX(r.started) AS cutoff FROM source_checks s JOIN runs r ON r.id=s.run_id
                            WHERE s.source_id=? AND s.status='complete' AND r.status IN ('complete','partial')""", (source["id"],)).fetchone()
        cutoffs[source["id"]] = row["cutoff"]
        latest = db.execute("""SELECT s.status,s.detail,s.urls,r.started FROM source_checks s JOIN runs r ON r.id=s.run_id
                              WHERE s.source_id=? ORDER BY r.started DESC LIMIT 1""", (source["id"],)).fetchone()
        if latest:
            previous_checks[source["id"]] = {**dict(latest), "urls": json.loads(latest["urls"])}
    inventory = {row["url"]: row["first_seen"] for row in db.execute("SELECT url,first_seen FROM inventory")}
    return {"previous_properties": previous, "legacy_reports": legacy, "last_successful_check": cutoffs,
            "max_price_gbp": MAX_PRICE_GBP, "price_expansions": price_expansions,
            "previous_inventory_urls": inventory,
            "previous_source_checks": previous_checks}


def inventory_urls(job):
    """Extract individual listing URL hints from flat or parallel-worker evidence."""
    found = set()
    for path in (job / "evidence").rglob("*.json"):
        try:
            snapshot = json.loads(path.read_text())
            if snapshot.get("status") != 200 or snapshot.get("error"):
                continue
            urls = [snapshot.get("url", "")] + [item.get("url", "") for item in snapshot.get("links", [])]
            for url in urls:
                if not re.search(r"/(?:properties/\d|(?:for-sale/)?details/\d|property/[^/?]+|properties-for-sale/property/|[^/?]+/plot-\d)", url):
                    continue
                try:
                    key = canonical_url(url)
                except ValueError:
                    continue
                found.add(key)
        except (ValueError, TypeError, OSError):
            continue
    return found


def remember_inventory(db, job, started):
    """Keep first-observation hints separate from verified/reportable properties."""
    for key in inventory_urls(job):
        db.execute("INSERT INTO inventory VALUES (?,?,?) ON CONFLICT(url) DO UPDATE SET last_seen=excluded.last_seen", (key, started, started))


def ingest(db, result, job, sources, run_id, day, started):
    source_ids = {s["id"] for s in sources}
    checks = result.get("source_checks", [])
    if len(checks) != len(source_ids) or {c["source_id"] for c in checks} != source_ids:
        raise ValueError("Research must return exactly one check for each of the 24 sources")
    if any(c["status"] not in {"complete", "partial", "failed"} or not c["detail"].strip() for c in checks):
        raise ValueError("Invalid source-check status")
    legacy_ids = {r[0] for r in db.execute("SELECT id FROM legacy")}
    accepted = duplicates = rejected = 0
    with db:
        for check in checks:
            db.execute("INSERT INTO source_checks VALUES (?,?,?,?,?)", (run_id, check["source_id"], check["status"], check["detail"], json.dumps(check["urls"])))
        for candidate in result.get("candidates", []):
            try:
                validate_candidate(candidate, job, source_ids, legacy_ids, datetime.fromisoformat(started).date())
                keys = identities(candidate)
                matches = {row[0] for key in keys for row in db.execute("SELECT property_id FROM aliases WHERE alias=?", (key,))}
                # Workers share yesterday's context, not each other's new discoveries.
                # Compare with central state after every accepted candidate as well.
                matches.update(row["id"] for row in db.execute("SELECT id,data FROM properties")
                               if same_addressed_home(candidate, json.loads(row["data"])))
                legacy = db.execute("SELECT matched_property_id FROM legacy WHERE id=?", (candidate["prior_report_id"],)).fetchone()
                if legacy and legacy[0]:
                    matches.add(legacy[0])
                if len(matches) > 1:
                    raise ValueError("Conflicting identities need review")
                identity = next(iter(matches), property_id(candidate))
                observed = [row[0] for key in keys if key.startswith("url:")
                            for row in db.execute("SELECT first_seen FROM inventory WHERE url=?", (key[4:],))]
                first_seen = min([started, *observed])
                old = db.execute("SELECT data FROM properties WHERE id=?", (identity,)).fetchone()
                if old:
                    candidate = merge_property_data(json.loads(old[0]), candidate)
                db.execute("""INSERT INTO properties VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE
                              SET data=excluded.data,last_seen=excluded.last_seen""", (identity, json.dumps(candidate), first_seen, started))
                for key in keys:
                    db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (key, identity))
                if candidate["prior_report_id"]:
                    db.execute("UPDATE legacy SET matched_property_id=? WHERE id=?", (identity, candidate["prior_report_id"]))
                if old or candidate["prior_report_id"]:
                    duplicates += 1
                    # Attach newly found listing links to the original archive entry,
                    # retaining its report day and historical price/description.
                    for report in db.execute("SELECT day,data FROM reports WHERE property_id=?", (identity,)).fetchall():
                        archived = json.loads(report["data"])
                        enriched = merge_property_data(archived, candidate)
                        for key in ("url", "portal_fallback", "fallback_reason", "agent", "agent_ref", "additional_urls"):
                            archived[key] = enriched[key]
                        for key in ("postcode", "listed_date"):
                            archived[key] = archived[key] or enriched[key]
                        db.execute("UPDATE reports SET data=? WHERE day=? AND property_id=?", (json.dumps(archived), report["day"], identity))
                else:
                    db.execute("INSERT INTO reports VALUES (?,?,?)", (day, identity, json.dumps(candidate)))
                    accepted += 1
            except (ValueError, KeyError, TypeError, OSError) as exc:
                rejected += 1
                db.execute("INSERT INTO rejected VALUES (?,?,?,?)", (run_id, candidate.get("address", ""), str(exc), json.dumps(candidate)))
                for source_id in candidate.get("source_ids", []):
                    db.execute("UPDATE source_checks SET status='partial',detail=detail || '; candidate needs validation review' WHERE run_id=? AND source_id=? AND status='complete'", (run_id, source_id))
        status = "complete" if all(c["status"] == "complete" for c in checks) and not rejected else "partial"
        if all(c["status"] == "failed" for c in checks):
            status = "failed"
        remember_inventory(db, job, started)
        # Partial checks must keep the newly eligible band pending for recovery.
        for check in db.execute("SELECT source_id FROM source_checks WHERE run_id=? AND status='complete'", (run_id,)).fetchall():
            db.execute("INSERT INTO source_price_coverage VALUES (?,?) ON CONFLICT(source_id) DO UPDATE SET max_price_gbp=MAX(max_price_gbp,excluded.max_price_gbp)", (check[0], MAX_PRICE_GBP))
        db.execute("UPDATE runs SET finished=?,status=? WHERE id=?", (now(), status, run_id))
    return {"new": accepted, "duplicates": duplicates, "rejected": rejected, "status": status}
