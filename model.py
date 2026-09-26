"""Structured research contract and deterministic validation/deduplication."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
NULL_STRING = {"type": ["string", "null"]}
LINK = obj({"label": STRING, "url": STRING})
EVIDENCE = obj({"file": STRING, "field": {"type": "string", "enum": ["address", "price", "bedrooms", "availability", "date"]}, "quote": STRING})
CANDIDATE = obj({
    "address": STRING, "postcode": STRING, "locality": {"type": "string", "enum": ["Dundee", "Broughty Ferry"]},
    "bedrooms": {"type": "integer"}, "price_gbp": {"type": "integer"}, "price_qualifier": STRING,
    "property_type": STRING, "agent": STRING, "agent_ref": STRING, "url": STRING,
    "portal_fallback": {"type": "boolean"}, "fallback_reason": STRING,
    "additional_urls": {"type": "array", "items": LINK}, "listed_date": NULL_STRING,
    "listing_kind": {"type": "string", "enum": ["ordinary", "auction", "new_build"]},
    "plot_number": STRING, "notes": STRING, "prior_report_id": NULL_STRING, "identity_note": STRING,
    "source_ids": {"type": "array", "items": STRING}, "evidence": {"type": "array", "items": EVIDENCE},
})
SCHEMA = obj({"candidates": {"type": "array", "items": CANDIDATE}, "source_checks": {
    "type": "array", "items": obj({"source_id": STRING, "status": {"type": "string", "enum": ["complete", "partial", "failed"]},
                                   "detail": STRING, "urls": {"type": "array", "items": STRING}})}})


def normalize(s):
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def canonical_url(url):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Invalid listing URL")
    if not p.path.strip("/"):
        raise ValueError("A homepage is not an individual listing")
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_") and k not in {"fbclid", "gclid"}]
    return urlunsplit(("https", p.netloc.lower(), p.path.rstrip("/"), urlencode(sorted(query)), ""))


def listing_url(url):
    canonical = canonical_url(url)
    parsed = urlsplit(canonical)
    host = parsed.hostname.removeprefix("www.")
    patterns = {"rightmove.co.uk": r"^/properties/\d+", "zoopla.co.uk": r"^/for-sale/details/\d+",
                "primelocation.com": r"^/for-sale/details/\d+", "onthemarket.com": r"^/details/\d+"}
    if host in patterns and not re.match(patterns[host], parsed.path):
        raise ValueError("Portal search page is not an individual listing")
    return canonical


def identities(p):
    keys = {"url:" + listing_url(p["url"])}
    for link in p.get("additional_urls", []):
        keys.add("url:" + listing_url(link["url"]))
    if p.get("agent_ref"):
        keys.add("ref:" + normalize(p["agent"]) + ":" + normalize(p["agent_ref"]))
    # A complete numbered address distinguishes units; never collapse a street-only hint.
    if p.get("postcode") and re.search(r"\d", p["address"]):
        keys.add("address:" + normalize(p["address"]) + ":" + normalize(p["postcode"]))
    if p.get("plot_number"):
        keys.add("plot:" + normalize(p["agent"] + p["address"]) + ":" + normalize(p["plot_number"]))
    return keys


def addressed_home(p):
    """A conservative address fallback: never infer an undisclosed flat/unit."""
    address = unicodedata.normalize("NFKC", p["address"]).casefold()
    address = re.sub(r"\b[a-z]{1,2}\d[a-z\d]?\s*\d[a-z]{2}\b", "", address)
    # Preserve unit separators: 1/23 must not become 12/3 or 123.
    address = " ".join(re.sub(r"[^\w/-]+", " ", address).split())
    address = re.sub(r"\s*([/-])\s*", r"\1", address)
    kind = p["property_type"].casefold()
    if re.search(r"\b(flat|apartment|maisonette)\b", kind):
        # Require separate unit/building numbers, or an explicit numbered suffix
        # or slash form. 'Flat at 16 ...' and 'Flat 2, Street ...' aren't enough.
        if not re.match(r"(?:(?:flat|apartment|unit) (?:[a-z]|\d+[a-z]?) \d+[a-z]? |\d+[a-z] |\d+[a-z]?/\d+[a-z]? )[a-z]", address):
            return None
        family = "unit"
    elif re.search(r"\b(house|bungalow|cottage|townhouse|detached|terraced)\b", kind):
        family = "house"
    else:
        return None
    if not re.match(r"\d+[a-z]?(?:[/\-]\d+[a-z]?)?\s+[a-z]", address) and family != "unit":
        return None
    return (p["locality"], address, family, normalize(p["agent"]), p["bedrooms"])


def same_addressed_home(left, right):
    """Corroborated full address, including unit, without requiring a postcode."""
    key = addressed_home(left)
    if key is None or key != addressed_home(right):
        return False
    if left.get("postcode") and right.get("postcode") and normalize(left["postcode"]) != normalize(right["postcode"]):
        return False
    if left.get("plot_number", "") != right.get("plot_number", ""):
        return False
    return True


def merge_property_data(old, new):
    """Combine identities without replacing an original agent with a portal."""
    merged = dict(new)
    primary = old if not old["portal_fallback"] and new["portal_fallback"] else new
    for key in ("url", "portal_fallback", "fallback_reason", "agent", "agent_ref"):
        merged[key] = primary[key]
    merged["postcode"] = new["postcode"] or old["postcode"]
    merged["listed_date"] = new["listed_date"] or old["listed_date"]
    merged["source_ids"] = sorted(set(old["source_ids"] + new["source_ids"]))
    links = {}
    primary_url = canonical_url(merged["url"])
    for p in (old, new):
        for link in [{"url": p["url"], "label": p["agent"]}, *p["additional_urls"]]:
            key = canonical_url(link["url"])
            if key != primary_url:
                links[key] = link
    merged["additional_urls"] = list(links.values())
    return merged


def validate_candidate(p, job, source_ids, legacy_ids, run_date):
    if set(p) != set(CANDIDATE["properties"]):
        raise ValueError("Candidate fields do not match schema")
    if type(p["bedrooms"]) is not int or p["bedrooms"] not in {2, 3}:
        raise ValueError("Bedrooms outside 2–3")
    if type(p["price_gbp"]) is not int or not 0 < p["price_gbp"] <= 260000:
        raise ValueError("Price outside cap")
    if p["locality"] not in {"Dundee", "Broughty Ferry"}:
        raise ValueError("Out of area")
    if not p["address"].strip() or not p["agent"].strip():
        raise ValueError("Missing address or agent")
    if not p["source_ids"] or not set(p["source_ids"]) <= source_ids:
        raise ValueError("Unknown discovery source")
    if p["prior_report_id"] and (p["prior_report_id"] not in legacy_ids or not p["identity_note"].strip()):
        raise ValueError("Unsubstantiated historical match")
    if p["portal_fallback"] and not p["fallback_reason"].strip():
        raise ValueError("Missing agent-link fallback explanation")
    if p["listing_kind"] == "new_build" and not p["plot_number"].strip():
        raise ValueError("A new-build candidate needs an individual plot")
    identities(p)  # Validate every link before it can reach HTML.
    fields = set()
    evidence_urls = set()
    for item in p["evidence"]:
        path = (job / item["file"]).resolve()
        if not path.is_relative_to((job / "evidence").resolve()) or not path.is_file():
            raise ValueError("Missing local evidence snapshot")
        snapshot = json.loads(path.read_text())
        if snapshot.get("status") != 200 or snapshot.get("error"):
            raise ValueError("Evidence page was not accessible")
        if datetime.fromisoformat(snapshot["retrieved_at"]).date() < run_date:
            raise ValueError("Evidence snapshot is not current")
        for url_field in ("url", "final_url"):
            if snapshot.get(url_field):
                try:
                    evidence_urls.add(canonical_url(snapshot[url_field]))
                except ValueError:
                    pass  # A homepage can corroborate facts, but cannot be the primary listing.
        quote = " ".join(item["quote"].split())
        text = " ".join(snapshot.get("text", "").split())
        if len(quote) < 3 or quote not in text:
            raise ValueError("Evidence excerpt does not occur in saved page")
        field = item["field"]
        if field == "price" and str(p["price_gbp"]) not in re.sub(r"[,\s]", "", quote):
            raise ValueError("Price evidence does not contain the advertised price")
        if field == "bedrooms" and not re.search(rf"\b(?:{p['bedrooms']}|{'two' if p['bedrooms'] == 2 else 'three'})\b", quote, re.I):
            raise ValueError("Bedroom evidence does not contain the bedroom count")
        if field == "availability" and re.search(r"\b(sold|reserved|withdrawn|under offer|sstc|let agreed)\b", quote, re.I):
            raise ValueError("Listing is not currently available")
        fields.add(field)
    if not {"price", "bedrooms", "address", "availability"} <= fields:
        raise ValueError("Incomplete evidence for core listing facts")
    if canonical_url(p["url"]) not in evidence_urls:
        raise ValueError("Primary listing link has no inspected evidence page")
    if p["listed_date"]:
        if date.fromisoformat(p["listed_date"]) > run_date or "date" not in fields:
            raise ValueError("Unverified or future listing date")


def property_id(p):
    return hashlib.sha256(canonical_url(p["url"]).encode()).hexdigest()[:20]
