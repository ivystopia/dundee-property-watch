#!/usr/bin/env python3
"""Replay matched private research trials; never publish or open production SQLite."""
import argparse
from collections import Counter
from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import signal
import time
from zoneinfo import ZoneInfo

import watch
from native_job import prepare_assignments, merge_assignments
from model import CANDIDATE, MAX_PRICE_GBP, identities
from render import render
from store import connect, ingest, now


def prepare(destination, baseline):
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    fixture = destination / "fixture"
    fixture.mkdir(mode=0o700)
    history = json.loads((baseline / "context.json").read_text())
    for name in ("context.json", "sources.json", "schema.json"):
        shutil.copyfile(baseline / name, fixture / name)
    shutil.copyfile(watch.ROOT / "criteria.md", fixture / "criteria.md")
    for records in history["initial_pages"].values():
        for record in records:
            relative = Path(record["file"])
            source = (baseline / relative).resolve()
            if not source.is_relative_to((baseline / "evidence").resolve()):
                raise ValueError("Fixture evidence escapes baseline")
            target = fixture / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    config = json.loads((watch.ROOT / "settings.json").read_text())
    config.update(model="gpt-6.1-sol", reasoning_effort="high", service_tier="default")
    watch.atomic_json(fixture / "settings.json", config)
    watch.atomic_json(destination / "manifest.json", {"created_at": now(), "baseline": str(baseline), "settings": config,
        "price_cap_gbp": MAX_PRICE_GBP, "sources": 24, "note": "Same pre-run history and entry snapshots; follow-up pages remain live. No production writes.",
        "fixture_sha256": {str(p.relative_to(fixture)): hashlib.sha256(p.read_bytes()).hexdigest() for p in fixture.rglob("*") if p.is_file()},
        "code_sha256": {name: hashlib.sha256((watch.ROOT / name).read_bytes()).hexdigest() for name in ("watch.py", "model.py", "store.py", "research.md", "native_complete.py", "native_job.py")}})
    return destination


def seed(db, history, sources):
    with db:
        for p in history["previous_properties"]:
            data = {k: p[k] for k in CANDIDATE["properties"]}
            db.execute("INSERT INTO properties VALUES (?,?,?,?)", (p["id"], json.dumps(data), p["first_seen"], p["first_seen"]))
            for alias in identities(p):
                db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (alias, p["id"]))
        for p in history["legacy_reports"]:
            db.execute("INSERT INTO legacy VALUES (?,?,?)", (p["id"], json.dumps(p), p["matched_property_id"]))
        for url, first_seen in history["previous_inventory_urls"].items():
            db.execute("INSERT INTO inventory VALUES (?,?,?)", (url, first_seen, first_seen))
        for source in sources:
            source_id = source["id"]
            cutoff = history["last_successful_check"].get(source_id)
            if cutoff:
                run_id = "baseline-" + source_id
                db.execute("INSERT INTO runs(id,started,finished,day,status,published) VALUES (?,?,?,?,'complete',?)", (run_id, cutoff, cutoff, cutoff[:10], cutoff))
                db.execute("INSERT INTO source_checks VALUES (?,?,?,'Baseline cutoff','[]')", (run_id, source_id, "complete"))
            expansion = history.get("price_expansions", {}).get(source_id)
            covered = expansion["min_price_gbp"] - 1 if expansion else MAX_PRICE_GBP
            db.execute("INSERT INTO source_price_coverage VALUES (?,?)", (source_id, covered))


def setup_trial(destination, pair, backend):
    fixture = destination / "fixture"
    manifest = json.loads((destination / "manifest.json").read_text())
    for name, checksum in manifest["code_sha256"].items():
        if hashlib.sha256((watch.ROOT / name).read_bytes()).hexdigest() != checksum:
            raise ValueError("Research code changed after benchmark preparation")
    job = destination / f"pair-{pair:02d}-{backend}"
    job.mkdir(mode=0o700, exist_ok=False)
    for name in ("context.json", "sources.json", "schema.json", "criteria.md", "settings.json"):
        shutil.copyfile(fixture / name, job / name)
    shutil.copytree(fixture / "evidence", job / "evidence")
    history = json.loads((job / "context.json").read_text())
    sources = json.loads((job / "sources.json").read_text())
    with connect(job / "validation-state") as db:
        seed(db, history, sources)
        db.execute("INSERT INTO runs(id,started,day,status) VALUES (?,?,?,'running')", (job.name, history["started"], history["report_day"]))
    watch.atomic_json(job / "trial.json", {"backend": backend, "pair": pair, "started_unix": time.time(), "started_at": now()})
    print(f"TRIAL_JOB={job}", flush=True)
    return job


def finish_trial(job, *, checkpoints=False):
    meta = json.loads((job / "trial.json").read_text())
    history = json.loads((job / "context.json").read_text())
    sources = json.loads((job / "sources.json").read_text())
    config = json.loads((job / "settings.json").read_text())
    if (job / "summary.json").exists():
        raise ValueError("Benchmark trial already finalized")
    if meta["backend"] == "native":
        result = merge_assignments(job, allow_checkpoints=checkpoints)
    else:
        result = json.loads((job / "research-result.json").read_text())
    db = connect(job / "validation-state")
    try:
        counts = ingest(db, result, job, sources, job.name, history["report_day"], history["started"])
        render(db, job / "site")
        accepted = [json.loads(r[0]) for r in db.execute("SELECT data FROM reports WHERE day=?", (history["report_day"],))]
        rejected = [dict(r) for r in db.execute("SELECT address,reason FROM rejected WHERE run_id=?", (job.name,))]
        summary = {**meta, "finished_at": now(), "seconds": round(time.time() - meta["started_unix"], 3),
            "settings": config, "counts": counts, "accepted": accepted, "rejected": rejected,
            "source_status": dict(Counter(c["status"] for c in result["source_checks"])), "source_checks": result["source_checks"],
            "result_sha256": hashlib.sha256((job / "research-result.json").read_bytes()).hexdigest(), "published": False,
            "limitations": ["Live follow-up pages may change between trials", "Accepted records require evidence/novelty review"]}
        watch.atomic_json(job / "summary.json", summary)
        print(json.dumps({k: v for k, v in summary.items() if k not in {"accepted", "source_checks", "settings"}}), flush=True)
        return summary
    finally:
        db.close()


def trial(destination, pair, backend):
    job = setup_trial(destination, pair, backend)
    config = json.loads((job / "settings.json").read_text())
    if backend == "native":
        manifest = prepare_assignments(job, config)
        print(json.dumps({"manifest": str(job / "native-research.json"), "job": str(job)}), flush=True)
        return job
    def expired(signum, frame):
        watch.stop_processes()
        raise TimeoutError("Benchmark trial deadline reached")
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(3000)
    try:
        watch.research(job, config=config)
        finish_trial(job)
    finally:
        signal.alarm(0)
        watch.stop_processes()
    return job


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preparation = sub.add_parser("prepare")
    preparation.add_argument("destination", type=Path)
    preparation.add_argument("baseline", type=Path)
    replay = sub.add_parser("trial")
    replay.add_argument("destination", type=Path)
    replay.add_argument("--pair", type=int, required=True)
    replay.add_argument("--backend", choices=("cli", "native"), required=True)
    finishing = sub.add_parser("finish-trial")
    finishing.add_argument("job", type=Path)
    finishing.add_argument("--checkpoints", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare(args.destination, args.baseline))
    elif args.command == "finish-trial":
        finish_trial(args.job, checkpoints=args.checkpoints)
    else:
        trial(args.destination, args.pair, args.backend)
