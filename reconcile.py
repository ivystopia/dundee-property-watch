#!/usr/bin/env python3
"""Explicit, evidence-backed reconciliation of a current native run's aliases.

This is a corrective stage, not an automatic street-address matching rule.
Research results remain immutable. A private database backup precedes changes;
native finalization republishes the corrected report without reingestion.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import time

from model import canonical_url
from native_job import active_lease, locked_state
from store import now, reconcile_current_aliases
import watch


def validate_proof(proof_path, job, started):
    proof_path = proof_path.resolve()
    if not proof_path.is_relative_to((job / "workers").resolve()):
        raise ValueError("Reconciliation proof must remain in a worker directory")
    proof = json.loads(proof_path.read_text())
    urls = proof["urls"]
    evidence = proof["evidence"]
    if len(urls) != 2 or len(set(map(canonical_url, urls))) != 2 or len(evidence) != 2:
        raise ValueError("Reconciliation requires two distinct listing URLs and snapshots")
    quotes = []
    for url, item in zip(urls, evidence):
        path = (proof_path.parent / item["file"]).resolve()
        if not path.is_relative_to(proof_path.parent) or not path.is_file():
            raise ValueError("Reconciliation evidence must remain private in the worker directory")
        snapshot = json.loads(path.read_text())
        if snapshot.get("status") != 200 or snapshot.get("error"):
            raise ValueError("Reconciliation evidence was not accessible")
        if canonical_url(snapshot["url"]) != canonical_url(url):
            raise ValueError("Reconciliation evidence does not match its individual listing")
        if datetime.fromisoformat(snapshot["retrieved_at"]) < datetime.fromisoformat(started):
            raise ValueError("Reconciliation evidence is not from this run")
        quote = " ".join(item["quote"].split())
        if len(quote) < 100 or quote not in " ".join(snapshot["text"].split()):
            raise ValueError("Reconciliation requires a saved distinctive shared excerpt")
        quotes.append(quote)
    if quotes[0] != quotes[1]:
        raise ValueError("Reconciliation excerpts do not corroborate the same listing")
    return urls


def reconcile(job, proof_path, *, retry_failed_photos=False):
    manifest = json.loads((job / "native-research.json").read_text())
    state = Path(manifest["state"])
    if job.resolve().parent != (state / "runs").resolve() or job.name != manifest["run_id"]:
        raise ValueError("Job is outside its declared private state directory")
    if time.time() >= manifest["deadline_unix"]:
        raise ValueError("Reconciliation exceeds the native run deadline")
    with locked_state(state) as db:
        if active_lease(db):
            raise ValueError("Another native run is active")
        run = db.execute("SELECT * FROM runs WHERE id=?", (manifest["run_id"],)).fetchone()
        if not run:
            raise ValueError("Native run is missing")
        urls = validate_proof(proof_path, job, run["started"])
        backup = job / ("history-before-reconciliation-" + now().replace(":", "-") + ".sqlite3")
        with sqlite3.connect(backup) as destination:
            db.backup(destination)
        changed = reconcile_current_aliases(db, manifest["run_id"], urls)
        outcome = json.loads((job / "outcome.json").read_text())
        actual_new = db.execute("SELECT COUNT(*) FROM reports r JOIN properties p ON p.id=r.property_id WHERE r.day=? AND p.first_seen>=?", (run["day"], run["started"])).fetchone()[0]
        if outcome["counts"]["new"] != actual_new:
            outcome["counts"]["duplicates"] = (outcome["counts"]["duplicates"] or 0) + outcome["counts"]["new"] - actual_new
            outcome["counts"]["new"] = actual_new
            outcome["reconciled_at"] = now()
            watch.atomic_json(job / "outcome.json", outcome)
        watch.atomic_json(job / "reconciliation-audit.json", {"checked_at": now(), "proof": str(proof_path), "backup": str(backup), "changed": changed})
        if retry_failed_photos:
            watch.enrich_photos(db, day=run["day"], retry_failed=True)
        return {"changed": changed, "counts": outcome["counts"], "backup": str(backup)}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path)
    parser.add_argument("proof", type=Path)
    parser.add_argument("--retry-failed-photos", action="store_true")
    args = parser.parse_args()
    print(json.dumps(reconcile(args.job, args.proof, retry_failed_photos=args.retry_failed_photos)))
