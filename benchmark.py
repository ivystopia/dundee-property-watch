#!/usr/bin/env python3
"""Replay matched private trials; production SQLite is only an optional read-only backup source."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import signal
import sqlite3
import time

import watch
from native_job import prepare_assignments, merge_assignments
from model import CANDIDATE, MAX_PRICE_GBP, identities
from render import render
from store import connect, ingest, now


BENCHMARK_ROOT = watch.DEFAULT_STATE / "benchmarks"
MODELS = ("gpt-6.1-sol", "gpt-6.1-terra")
CODE_FILES = ("benchmark.py", "watch.py", "model.py", "store.py", "research.md", "criteria.md", "sources.json",
              "native_complete.py", "native_job.py", "identity_evidence.py", "render.py", "photos.py", "locations.py")


def private_path(path, *, tree=False):
    """Refuse production paths and symlink/hardlink escapes before any mutation."""
    path, root = Path(path).absolute(), BENCHMARK_ROOT.absolute()
    if ".." in path.parts or root.is_symlink() or path == root or not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Benchmark path must be a private benchmark descendant")
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Benchmark paths cannot contain symlinks")
    paths = [path, *(path.rglob("*") if tree and path.is_dir() else [])]
    for item in paths:
        if item.is_symlink() or (item.is_file() and item.stat().st_nlink != 1):
            raise ValueError("Benchmark files cannot be symlinks or hardlinks")
    return path


def selected_settings(config, model=None, coordinator_model=None, coordinator_effort="xhigh"):
    model = model or config.get("model", MODELS[0])
    coordinator_model = coordinator_model or model
    if model not in MODELS or coordinator_model not in MODELS:
        raise ValueError("Benchmark supports only GPT-6.1 Sol and Terra")
    if coordinator_effort != "xhigh":
        raise ValueError("Matched coordinator reasoning must remain xhigh")
    return {**config, "model": model, "reasoning_effort": "high", "service_tier": "default",
            "coordinator_model": coordinator_model, "coordinator_reasoning_effort": coordinator_effort}


def verify_fixture(destination):
    destination = private_path(destination, tree=True)
    manifest = json.loads((destination / "manifest.json").read_text())
    fixture = destination / "fixture"
    if {str(p.relative_to(fixture)) for p in fixture.rglob("*") if p.is_file()} != set(manifest["fixture_sha256"]):
        raise ValueError("Benchmark fixture file set changed after preparation")
    for name, checksum in manifest["fixture_sha256"].items():
        path = private_path(fixture / name)
        if not path.is_relative_to(fixture) or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError("Benchmark fixture changed after preparation")
    for name, checksum in manifest["code_sha256"].items():
        path = watch.ROOT / name
        if not path.resolve().is_relative_to(watch.ROOT.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError("Research code changed after benchmark preparation")
    return manifest


def prepare(destination, baseline, *, history_db=None):
    destination = private_path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    fixture = destination / "fixture"
    fixture.mkdir(mode=0o700)
    (fixture / "evidence").mkdir(mode=0o700)
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
            target = private_path(fixture / relative)
            if not target.is_relative_to(fixture / "evidence"):
                raise ValueError("Fixture evidence destination escapes fixture")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    config = json.loads((watch.ROOT / "settings.json").read_text())
    config = selected_settings(config, model=MODELS[0])
    watch.atomic_json(fixture / "settings.json", config)
    if history_db is not None:
        source = Path(history_db).resolve(strict=True)
        target = fixture / "history.sqlite3"
        # SQLite backup includes committed WAL contents and every table, unlike copying the DB file.
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(target)) as snapshot:
            original.backup(snapshot)
            snapshot.execute("PRAGMA journal_mode=DELETE")
        target.chmod(0o600)
    watch.atomic_json(destination / "manifest.json", {"created_at": now(), "baseline": str(baseline), "settings": config,
        "history_db": str(history_db) if history_db else None,
        "price_cap_gbp": MAX_PRICE_GBP, "sources": len(json.loads((fixture / "sources.json").read_text())),
        "note": "Same pre-run history and entry snapshots; follow-up pages remain live. No production writes. A supplied database must correspond to the supplied context.",
        "fixture_sha256": {str(p.relative_to(fixture)): hashlib.sha256(p.read_bytes()).hexdigest() for p in fixture.rglob("*") if p.is_file()},
        "code_sha256": {name: hashlib.sha256((watch.ROOT / name).read_bytes()).hexdigest() for name in CODE_FILES}})
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


def setup_trial(destination, pair, backend, *, model=None, coordinator_model=None, coordinator_effort="xhigh"):
    destination = private_path(destination, tree=True)
    if type(pair) is not int or pair < 1 or backend not in {"cli", "native"}:
        raise ValueError("Invalid benchmark pair or backend")
    fixture = destination / "fixture"
    verify_fixture(destination)
    config = selected_settings(json.loads((fixture / "settings.json").read_text()), model, coordinator_model, coordinator_effort)
    suffix = f"-{config['model']}-coordinator-{config['coordinator_model']}" if model or coordinator_model else ""
    job = private_path(destination / f"pair-{pair:02d}-{backend}{suffix}")
    job.mkdir(mode=0o700, exist_ok=False)
    for name in ("context.json", "sources.json", "schema.json", "criteria.md", "settings.json"):
        shutil.copyfile(fixture / name, job / name)
    shutil.copytree(fixture / "evidence", job / "evidence")
    watch.atomic_json(job / "settings.json", config)
    history = json.loads((job / "context.json").read_text())
    sources = json.loads((job / "sources.json").read_text())
    state = job / "validation-state"
    state.mkdir(mode=0o700)
    if (fixture / "history.sqlite3").exists():
        shutil.copyfile(fixture / "history.sqlite3", state / "history.sqlite3")
    with closing(connect(state)) as db, db:
        if not (fixture / "history.sqlite3").exists():
            seed(db, history, sources)
        db.execute("INSERT INTO runs(id,started,day,status) VALUES (?,?,?,'running')", (job.name, history["started"], history["report_day"]))
    watch.atomic_json(job / "trial.json", {"backend": backend, "pair": pair, "started_unix": time.time(), "started_at": now(),
        "requested_settings": config, "published": False})
    print(f"TRIAL_JOB={job}", flush=True)
    return job


def finish_trial(job, *, checkpoints=False):
    job = private_path(job, tree=True)
    verify_fixture(job.parent)
    meta = json.loads((job / "trial.json").read_text())
    history = json.loads((job / "context.json").read_text())
    sources = json.loads((job / "sources.json").read_text())
    config = json.loads((job / "settings.json").read_text())
    if (job / "summary.json").exists():
        raise ValueError("Benchmark trial already finalized")
    if meta["backend"] == "native":
        manifest = json.loads((job / "native-research.json").read_text())
        for worker in manifest["workers"]:
            if not private_path(worker["job"]).is_relative_to(job / "workers"):
                raise ValueError("Worker job escapes isolated trial")
        result = merge_assignments(job, allow_checkpoints=checkpoints)
    else:
        result = json.loads((job / "research-result.json").read_text())
    db = connect(job / "validation-state")
    try:
        previous_reports = {(r[0], r[1]) for r in db.execute("SELECT day,property_id FROM reports")}
        counts = ingest(db, result, job, sources, job.name, history["report_day"], history["started"])
        render(db, job / "site")
        accepted = [json.loads(r["data"]) for r in db.execute("SELECT day,property_id,data FROM reports WHERE day=?", (history["report_day"],))
                    if (r["day"], r["property_id"]) not in previous_reports]
        rejected = [dict(r) for r in db.execute("SELECT address,reason FROM rejected WHERE run_id=?", (job.name,))]
        validated_checks = [dict(r) for r in db.execute("SELECT source_id,status,detail FROM source_checks WHERE run_id=?", (job.name,))]
        summary = {**meta, "finished_at": now(), "seconds": round(time.time() - meta["started_unix"], 3),
            "settings": config, "observed_execution": json.loads((job / "execution.json").read_text()) if (job / "execution.json").exists() else None,
            "counts": counts, "accepted": accepted, "rejected": rejected,
            "source_status": dict(Counter(c["status"] for c in result["source_checks"])), "source_checks": result["source_checks"],
            "validated_source_status": dict(Counter(c["status"] for c in validated_checks)), "validated_source_checks": validated_checks,
            "result_sha256": hashlib.sha256((job / "research-result.json").read_bytes()).hexdigest(), "published": False,
            "limitations": ["Live follow-up pages may change between trials", "Accepted records require evidence/novelty review"]}
        watch.atomic_json(job / "summary.json", summary)
        print(json.dumps({k: v for k, v in summary.items() if k not in {"accepted", "source_checks", "settings"}}), flush=True)
        return summary
    finally:
        db.close()


def trial(destination, pair, backend, *, model=None, coordinator_model=None, coordinator_effort="xhigh"):
    job = setup_trial(destination, pair, backend, model=model, coordinator_model=coordinator_model, coordinator_effort=coordinator_effort)
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
    preparation.add_argument("--history-db", type=Path, help="Read-only SQLite backup source matching baseline context")
    replay = sub.add_parser("trial")
    replay.add_argument("destination", type=Path)
    replay.add_argument("--pair", type=int, required=True)
    replay.add_argument("--backend", choices=("cli", "native"), required=True)
    replay.add_argument("--model", choices=MODELS, help="Researcher model; explicit selection adds model identifiers to trial name")
    replay.add_argument("--coordinator-model", choices=MODELS)
    replay.add_argument("--coordinator-effort", choices=("xhigh",), default="xhigh")
    finishing = sub.add_parser("finish-trial")
    finishing.add_argument("job", type=Path)
    finishing.add_argument("--checkpoints", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare(args.destination, args.baseline, history_db=args.history_db))
    elif args.command == "finish-trial":
        finish_trial(args.job, checkpoints=args.checkpoints)
    else:
        trial(args.destination, args.pair, args.backend, model=args.model, coordinator_model=args.coordinator_model, coordinator_effort=args.coordinator_effort)
