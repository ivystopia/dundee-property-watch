#!/usr/bin/env python3
"""Small durable stages for the scheduled Codex coordinator; no resident supervisor."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import time
import uuid
from zoneinfo import ZoneInfo

import watch
from model import SCHEMA
from store import connect, context, ingest, inventory_urls, now


class StateBusy(RuntimeError):
    pass


def active_lease(db):
    return db.execute("SELECT * FROM native_leases WHERE status='active' AND deadline>?", (time.time(),)).fetchone()


def close_workers(job):
    for worker in (job / "workers").glob("worker-*"):
        watch.atomic_json(worker / "native-closed.json", {"closed_at": now()})


def recover_leases(db):
    for lease in db.execute("SELECT * FROM native_leases WHERE status='active' AND deadline<=?", (time.time(),)).fetchall():
        close_workers(Path(lease["job"]))
        with db:
            db.execute("UPDATE runs SET status='failed',finished=?,error='Native coordinator lease expired before finalization' WHERE id=? AND status='running'", (now(), lease["run_id"]))
            db.execute("UPDATE native_leases SET status='failed' WHERE run_id=?", (lease["run_id"],))


@contextmanager
def locked_state(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "watch.lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StateBusy("Another property-watch operation holds the state lock")
        db = connect(state)
        try:
            recover_leases(db)
            yield db
        finally:
            db.close()


def prepare_assignments(job, config, *, overall_deadline=None):
    concurrency = config.get("max_concurrent_research", 3)
    if type(concurrency) is not int or not 1 <= concurrency <= 3:
        raise ValueError("max_concurrent_research must be between 1 and 3")
    sources = json.loads((job / "sources.json").read_text())
    history = json.loads((job / "context.json").read_text())
    manifest = {"schema_version": 1, "job": str(job), "settings": config, "status": "prepared",
                "deadline_unix": overall_deadline or time.time() + 3000, "workers": []}
    for index, group in enumerate(watch.partition_sources(sources), 1):
        worker = watch.prepare_worker(job, f"worker-{index}", group, history)
        scoped = json.loads((worker / "context.json").read_text())
        # Historical evidence quotes are neither current proof nor needed for identity.
        identity_fields = {"id", "first_seen", "address", "postcode", "locality", "bedrooms", "price_gbp",
                           "property_type", "agent", "agent_ref", "url", "additional_urls", "plot_number", "source_ids"}
        scoped["previous_properties"] = [{k: v for k, v in p.items() if k in identity_fields} for p in scoped["previous_properties"]]
        scoped["history_projection"] = "Exact property identities and observation history; old evidence excerpts omitted"
        watch.atomic_json(worker / "context.json", scoped)
        watch.atomic_json(worker / "settings.json", config)
        (worker / "prompt.txt").write_text(watch.worker_prompt(worker, config, native=True))
        manifest["workers"].append({"name": worker.name, "job": str(worker), "source_ids": [s["id"] for s in group],
                                   "prompt_file": str(worker / "prompt.txt"), "status": "queued"})
    watch.atomic_json(job / "native-research.json", manifest)
    return manifest


def start_worker(worker):
    job = worker.parent.parent
    manifest = json.loads((job / "native-research.json").read_text())
    slot = next(w for w in manifest["workers"] if Path(w["job"]) == worker)
    if manifest["status"] != "prepared" or time.time() >= manifest["deadline_unix"] or (worker / "native-closed.json").exists():
        raise ValueError("Native job is closed or expired")
    if (worker / "native-assignment.json").exists():
        raise ValueError("Native worker has already started")
    assignment = {"job_id": uuid.uuid4().hex, "deadline_unix": min(time.time() + manifest["settings"]["research_timeout_seconds"], manifest["deadline_unix"])}
    watch.atomic_json(worker / "native-assignment.json", assignment)
    return {**slot, **assignment}


def merge_assignments(job, *, allow_checkpoints=False):
    manifest = json.loads((job / "native-research.json").read_text())
    if manifest["status"] == "sealed":
        return json.loads((job / "research-result.json").read_text())
    if manifest["status"] != "prepared":
        raise ValueError("Native results already finalized")
    pending = [Path(w["job"]) for w in manifest["workers"] if not (Path(w["job"]) / "native-complete.json").exists()]
    for worker in pending:
        assignment = worker / "native-assignment.json"
        deadline = json.loads(assignment.read_text())["deadline_unix"] if assignment.exists() else manifest["deadline_unix"]
        if not allow_checkpoints and time.time() < deadline:
            raise ValueError("Worker still active; interrupt it before checkpoint finalization")
    close_workers(job)
    sources = json.loads((job / "sources.json").read_text())
    merged = {"candidates": [], "source_checks": []}
    for slot in manifest["workers"]:
        worker = Path(slot["job"])
        assigned = [s for s in sources if s["id"] in slot["source_ids"]]
        try:
            marker = worker / "native-complete.json"
            if marker.exists():
                completion = json.loads(marker.read_text())
                assignment = json.loads((worker / "native-assignment.json").read_text())
                if completion.get("job_id") != assignment["job_id"] or completion.get("finished_unix", float("inf")) > assignment["deadline_unix"]:
                    raise ValueError("Invalid or late native completion")
                result_worker = worker / "native-sealed"
                result_path = result_worker / "research-result.json"
                slot["status"] = "complete"
            else:
                result_path = worker / "research-result.json"
                result_worker = worker
                slot["status"] = "checkpoint"
            result = watch.merge_worker(job, result_worker, assigned, json.loads(result_path.read_text()), destination_name=worker.name)
        except Exception as exc:
            detail = f"Native worker failed: {type(exc).__name__}: {exc}"
            (worker / "worker-error.txt").write_text(detail + "\n")
            result = watch.failed_result(assigned, detail)
            slot.update(status="failed", detail=detail)
        for key in merged:
            merged[key].extend(result[key])
    watch.atomic_json(job / "research-result.json", merged)
    manifest["status"] = "sealed"
    watch.atomic_json(job / "native-research.json", manifest)
    return merged


def prepare(state, site, *, scheduled=True, should_publish=True, overall_timeout_seconds=3000):
    if type(overall_timeout_seconds) is not int or overall_timeout_seconds <= 0:
        raise ValueError("Overall timeout must be a positive integer")
    try:
        return _prepare(state, site, scheduled=scheduled, should_publish=should_publish,
                        overall_timeout_seconds=overall_timeout_seconds)
    except StateBusy:
        return {"status": "busy"}


def _prepare(state, site, *, scheduled, should_publish, overall_timeout_seconds):
    with locked_state(state) as db:
        if active_lease(db):
            return {"status": "busy"}
        watch.SITE = site
        if should_publish and db.execute("SELECT 1 FROM runs WHERE status IN ('complete','partial') AND published IS NULL").fetchone():
            watch.publish(db)
        started = now()
        day = datetime.fromisoformat(started).astimezone(ZoneInfo("Europe/London")).date().isoformat()
        if scheduled and db.execute("SELECT 1 FROM runs WHERE day=? AND status IN ('complete','partial') AND published IS NOT NULL", (day,)).fetchone():
            return {"status": "already_published", "day": day}
        with db:
            db.execute("UPDATE runs SET status='failed',finished=?,error='Interrupted CLI job' WHERE status='running'", (started,))
        run_id = datetime.fromisoformat(started).strftime("%Y%m%dT%H%M%S%fZ")
        job = state / "runs" / run_id
        job.mkdir(mode=0o700, parents=True)
        deadline = time.time() + overall_timeout_seconds
        with db:
            db.execute("INSERT INTO runs(id,started,day,status) VALUES (?,?,?,'running')", (run_id, started, day))
            db.execute("INSERT INTO native_leases VALUES (?,?,?,'active')", (run_id, str(job), deadline))
        try:
            for name in ("sources.json", "criteria.md"):
                shutil.copyfile(watch.ROOT / name, job / name)
            watch.atomic_json(job / "schema.json", SCHEMA)
            sources = json.loads((job / "sources.json").read_text())
            history = context(db, sources)
            history.update(run_id=run_id, started=started, report_day=day, initial_pages=watch.collect(sources, job))
            inventory = inventory_urls(job)
            history.update(current_inventory_urls=sorted(inventory), new_inventory_urls=sorted(inventory - set(history["previous_inventory_urls"])))
            watch.atomic_json(job / "context.json", history)
            config = json.loads((watch.ROOT / "settings.json").read_text())
            config["service_tier"] = "default"
            manifest = prepare_assignments(job, config, overall_deadline=deadline)
            manifest.update(state=str(state), site=str(site), run_id=run_id, should_publish=should_publish)
            watch.atomic_json(job / "native-research.json", manifest)
            return {"status": "prepared", "manifest": str(job / "native-research.json"), "job": str(job), "deadline_unix": deadline}
        except Exception as exc:
            close_workers(job)
            with db:
                db.execute("UPDATE runs SET status='failed',finished=?,error=? WHERE id=?", (now(), str(exc), run_id))
                db.execute("UPDATE native_leases SET status='failed' WHERE run_id=?", (run_id,))
            raise


def finalize(job, *, allow_checkpoints=False):
    manifest = json.loads((job / "native-research.json").read_text())
    state, site = Path(manifest["state"]), Path(manifest["site"])
    if job.resolve().parent != (state / "runs").resolve() or job.name != manifest["run_id"]:
        raise ValueError("Native job is outside its declared state directory")
    with locked_state(state) as db:
        lease = db.execute("SELECT * FROM native_leases WHERE run_id=?", (manifest["run_id"],)).fetchone()
        if not lease or lease["status"] == "failed":
            raise ValueError("Native job lease is missing or expired")
        watch.SITE = site
        if lease["status"] == "finalized":
            row = db.execute("SELECT published FROM runs WHERE id=?", (manifest["run_id"],)).fetchone()
            if manifest["should_publish"] and not row["published"]:
                watch.publish(db)
            return json.loads((job / "outcome.json").read_text())
        result = merge_assignments(job, allow_checkpoints=allow_checkpoints)
        history = json.loads((job / "context.json").read_text())
        sources = json.loads((job / "sources.json").read_text())
        run = db.execute("SELECT status FROM runs WHERE id=?", (manifest["run_id"],)).fetchone()
        if run["status"] in {"complete", "partial"}:
            counts = {"new": db.execute("SELECT COUNT(*) FROM reports WHERE day=?", (history["report_day"],)).fetchone()[0],
                      "duplicates": None, "rejected": db.execute("SELECT COUNT(*) FROM rejected WHERE run_id=?", (manifest["run_id"],)).fetchone()[0], "status": run["status"]}
        else:
            counts = ingest(db, result, job, sources, manifest["run_id"], history["report_day"], history["started"])
        outcome = {"run_id": manifest["run_id"], "counts": counts, "finished_at": now()}
        watch.atomic_json(job / "outcome.json", outcome)
        with db:
            db.execute("UPDATE native_leases SET status=? WHERE run_id=?", ("failed" if counts["status"] == "failed" else "finalized", manifest["run_id"]))
        if counts["status"] == "failed":
            raise RuntimeError("All sources failed; previous public report retained")
        watch.enrich_photos(db, day=history["report_day"])
        watch.cache_photos(db)
        watch.render(db, site)
        if manifest["should_publish"]:
            watch.publish(db)
        return outcome


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--state", type=Path, default=watch.DEFAULT_STATE)
    prep.add_argument("--site", type=Path, default=watch.SITE)
    prep.add_argument("--no-publish", action="store_true")
    start = sub.add_parser("start-worker")
    start.add_argument("worker", type=Path)
    finish = sub.add_parser("finalize")
    finish.add_argument("job", type=Path)
    finish.add_argument("--checkpoints", action="store_true")
    args = parser.parse_args()
    import signal
    signal.signal(signal.SIGALRM, lambda signum, frame: (_ for _ in ()).throw(TimeoutError("Native stage deadline reached")))
    remaining = 3000
    if args.command == "finalize":
        manifest = json.loads((args.job / "native-research.json").read_text())
        remaining = max(1, int(manifest["deadline_unix"] - time.time()))
    signal.alarm(remaining)
    if args.command == "prepare":
        output = prepare(args.state, args.site, should_publish=not args.no_publish)
    elif args.command == "start-worker":
        output = start_worker(args.worker)
    else:
        output = finalize(args.job, allow_checkpoints=args.checkpoints)
    print(json.dumps(output), flush=True)
