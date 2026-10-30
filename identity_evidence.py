"""Conservative cross-portal identities from saved individual descriptions."""
import hashlib
import json
import re
from urllib.parse import urlsplit

from model import canonical_url, normalize


def description_fingerprints(candidate, job):
    """Fail closed unless a known portal exposes a long, delimited description.

    Never hash whole pages: navigation, agent biographies and disclaimers are
    shared by unrelated listings. New-build house-type copy is also shared.
    """
    if candidate.get("listing_kind") != "ordinary" or candidate.get("plot_number"):
        return []
    fingerprints = []
    primary = canonical_url(candidate["url"])
    host = urlsplit(primary).hostname.removeprefix("www.")
    if host not in {"rightmove.co.uk", "zoopla.co.uk", "primelocation.com"}:
        return []
    for filename in {item["file"] for item in candidate.get("evidence", [])}:
        path = (job / filename).resolve()
        if not path.is_relative_to((job / "evidence").resolve()):
            continue
        try:
            snapshot = json.loads(path.read_text())
            if snapshot.get("status") != 200 or snapshot.get("error"):
                continue
            if primary not in {canonical_url(snapshot[k]) for k in ("url", "final_url") if snapshot.get(k)}:
                continue
            text = " ".join(snapshot["text"].split())
            heading = "Description" if host == "rightmove.co.uk" else "About this property"
            match = re.search(r"\b" + heading + r"\s+(.+?)\s+Read full description\b", text)
            if not match:
                continue
            description = match[1]
            # Zoopla/PrimeLocation place their feature list before the prose.
            # Only compare the common complete prose, starting at an opening
            # sentence also present in the Rightmove description.
            if host != "rightmove.co.uk":
                opener = re.search(r"\b(?:We are |This |Situated |Located |Welcome |Nestled )", description)
                if not opener:
                    continue
                description = description[opener.start():]
            description = " ".join(description.split()).casefold()
            street = normalize(candidate["address"].split(",")[0])
            if len(description.split()) < 100 or len(description) < 600 or street not in normalize(description):
                continue
            fingerprints.append((hashlib.sha256(description.encode()).hexdigest(), host, str(path)))
        except (ValueError, TypeError, KeyError, OSError):
            continue
    return fingerprints


def corroborating_facts(left, right):
    """A shared description alone never overrides conflicting property facts."""
    # Price is an observation, not an identity: the same home can be relisted.
    for field in ("address", "locality", "agent", "bedrooms", "property_type", "listing_kind", "plot_number"):
        a, b = left.get(field), right.get(field)
        if isinstance(a, str) and isinstance(b, str):
            a, b = normalize(a), normalize(b)
        if a != b:
            return False
    return not (left.get("postcode") and right.get("postcode") and
                normalize(left["postcode"]) != normalize(right["postcode"]))


def description_matches(db, candidate, fingerprints):
    matches = set()
    for digest, host, _ in fingerprints:
        for row in db.execute("""SELECT p.id,p.data FROM property_descriptions d
                                 JOIN properties p ON p.id=d.property_id
                                 WHERE d.digest=? AND d.host<>?""", (digest, host)):
            if corroborating_facts(candidate, json.loads(row["data"])):
                matches.add(row["id"])
    return matches


def remember_descriptions(db, identity, fingerprints):
    db.executemany("INSERT OR IGNORE INTO property_descriptions VALUES (?,?,?,?)",
                   [(identity, *item) for item in fingerprints])
