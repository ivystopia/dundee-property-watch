#!/usr/bin/env python3
"""Run the daily Dundee property watch, keeping research state private."""
from __future__ import annotations

import argparse
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from zoneinfo import ZoneInfo

from fetch import fetch
from model import SCHEMA
from pages import publish_site
from photos import cache_photos, enrich_photos
from render import render
from store import connect, context, import_export, ingest, inventory_urls, now


ROOT = Path(__file__).resolve().parent
REPO = ROOT
DEFAULT_STATE = Path.home() / ".local/state/dundee-property-watch"
SITE = ROOT / "site"


def log(text):
    print(f"{now()} {text}", flush=True)


def collect(sources, job):
    def collect_source(source):
        records = []
        for index, url in enumerate(source["urls"]):
            path = job / "evidence" / f"{source['id']}-{index}.json"
            record = fetch(url, path)
            records.append({"url": url, "file": str(path.relative_to(job)), "status": record["status"], "error": record["error"]})
        log(f"Fetched entry pages: {source['id']}")
        return source["id"], records
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(collect_source, sources))


def research_worker(job, config):
    effort = config["reasoning_effort"]
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unsupported reasoning effort in settings.json")
    command = [shutil.which("codex") or str(Path.home() / ".local/bin/codex"), "-a", "never", "exec",
               "--ignore-user-config", "--skip-git-repo-check", "--sandbox", "workspace-write",
               "-c", "sandbox_workspace_write.network_access=true", "-c", 'web_search="live"',
               "-c", f'model_reasoning_effort="{effort}"', "-c", "features.multi_agent=false",
               "--cd", str(job), "--json", "--output-last-message", str(job / "completion.txt")]
    if config.get("model"):
        command.extend(["--model", config["model"]])
    (job / "settings.json").write_text(json.dumps(config, indent=2) + "\n")
    prompt = (ROOT / "research.md").read_text().replace("{FETCH_HELPER}", str(ROOT / "fetch.py")).replace("{BROWSER_SKILL}", str(Path.home() / ".codex/skills/browse-with-firefox/SKILL.md")) + f"\nJOB={job}\nRead context.json, sources.json, schema.json and criteria.md in JOB. Current UTC time: {now()}. Research budget: {config['research_budget_minutes']} minutes. Prioritize finishing within this budget.\n"
    command.append("-")
    # The researcher doesn't need AWS credentials or arbitrary inherited secret variables.
    env = {k: v for k, v in os.environ.items() if k in {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TZ", "CODEX_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DUNDEE_TOR_PROXY"}}
    with (job / "events.jsonl").open("w") as events, (job / "codex.stderr").open("w") as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=events, stderr=errors, text=True,
                                   env=env, start_new_session=True)
        try:
            process.communicate(prompt, timeout=config["research_timeout_seconds"])
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            checkpoint = job / "research-result.json"
            if checkpoint.exists():
                log("Research time limit reached; validating saved checkpoint")
                return json.loads(checkpoint.read_text())
            raise RuntimeError("Research time limit reached without a saved checkpoint")
        if process.returncode:
            raise RuntimeError(f"Codex research failed (exit {process.returncode}); inspect private run logs")
    return json.loads((job / "research-result.json").read_text())


def partition_sources(sources):
    """Spread portals, agents, auctioneers and builders across three workers."""
    ids = [source["id"] for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate source IDs in registry")
    return [sources[index::3] for index in range(min(3, len(sources)))]


def failed_result(sources, detail):
    return {"candidates": [], "source_checks": [
        {"source_id": source["id"], "status": "failed", "detail": detail, "urls": []}
        for source in sources]}


def prepare_worker(job, name, sources, history):
    worker = job / "workers" / name
    (worker / "evidence").mkdir(parents=True, mode=0o700)
    ids = {source["id"] for source in sources}
    scoped = deepcopy(history)
    for field in ("initial_pages", "last_successful_check", "previous_source_checks"):
        scoped[field] = {key: value for key, value in scoped.get(field, {}).items() if key in ids}
    scoped["assigned_source_ids"] = sorted(ids)
    for records in scoped["initial_pages"].values():
        for record in records:
            original = (job / record["file"]).resolve()
            if not original.is_relative_to((job / "evidence").resolve()):
                raise ValueError("Initial snapshot is outside the evidence directory")
            relative = original.relative_to((job / "evidence").resolve())
            target = worker / "evidence" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            record["file"] = str(target.relative_to(worker))
    (worker / "context.json").write_text(json.dumps(scoped, indent=2))
    (worker / "sources.json").write_text(json.dumps(sources, indent=2))
    for filename in ("schema.json", "criteria.md"):
        shutil.copyfile(job / filename, worker / filename)
    (worker / "research-result.json").write_text(json.dumps(failed_result(sources, "Research has not completed")))
    return worker


def merge_worker(job, worker, sources, result):
    """Validate ownership and copy isolated evidence into the central run."""
    result = deepcopy(result)
    ids = {source["id"] for source in sources}
    checks = result["source_checks"]
    if len(checks) != len(ids) or {check["source_id"] for check in checks} != ids:
        raise ValueError("Worker must return exactly one check for each assigned source")
    for check in checks:
        if check["status"] not in {"complete", "partial", "failed"} or not check["detail"].strip():
            raise ValueError("Invalid worker source-check status")
        if not isinstance(check["urls"], list) or not all(isinstance(url, str) for url in check["urls"]):
            raise ValueError("Invalid worker source-check URLs")
    evidence_root = (worker / "evidence").resolve()
    destination = job / "evidence" / worker.name
    for candidate in result["candidates"]:
        if not candidate["source_ids"] or not set(candidate["source_ids"]) <= ids:
            raise ValueError("Worker candidate claims an unassigned discovery source")
        for item in candidate["evidence"]:
            original = (worker / item["file"]).resolve()
            if not original.is_relative_to(evidence_root) or not original.is_file():
                raise ValueError("Worker candidate evidence escapes its evidence directory or is missing")
            item["file"] = str((destination / original.relative_to(evidence_root)).relative_to(job))
    for original in evidence_root.rglob("*"):
        if not original.is_file():
            continue
        if not original.resolve().is_relative_to(evidence_root):
            raise ValueError("Worker evidence contains an external symlink")
        target = destination / original.relative_to(evidence_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
    return result


def research(job):
    config = json.loads((ROOT / "settings.json").read_text())
    concurrency = config.get("max_concurrent_research", 3)
    if type(concurrency) is not int or not 1 <= concurrency <= 3:
        raise ValueError("max_concurrent_research must be between 1 and 3")
    sources = json.loads((job / "sources.json").read_text())
    history = json.loads((job / "context.json").read_text())
    assignments = [(prepare_worker(job, f"worker-{index}", group, history), group)
                   for index, group in enumerate(partition_sources(sources), start=1)]
    (job / "settings.json").write_text(json.dumps(config, indent=2) + "\n")

    def work(assignment):
        worker, assigned = assignment
        try:
            result = merge_worker(job, worker, assigned, research_worker(worker, config))
            log(f"{worker.name} finished ({len(assigned)} sources)")
            return result
        except Exception as exc:
            detail = f"Research worker failed: {type(exc).__name__}: {exc}"
            (worker / "worker-error.txt").write_text(detail + "\n")
            log(f"{worker.name} failed; other workers' results will be retained")
            return failed_result(assigned, detail)

    merged = {"candidates": [], "source_checks": []}
    log(f"Starting {len(assignments)} research workers, at most {concurrency} concurrently")
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for result in pool.map(work, assignments):
            merged["candidates"].extend(result["candidates"])
            merged["source_checks"].extend(result["source_checks"])
    if len(merged["source_checks"]) != len(sources) or {check["source_id"] for check in merged["source_checks"]} != {source["id"] for source in sources}:
        raise ValueError("Merged research does not cover the source registry exactly once")
    temporary = job / "research-result.tmp"
    temporary.write_text(json.dumps(merged, indent=2) + "\n")
    temporary.replace(job / "research-result.json")
    return merged


def publish(db):
    enrich_photos(db)
    log("Thumbnail cache: " + json.dumps(cache_photos(db)))
    render(db, SITE)
    publication = publish_site(ROOT, SITE, log=log)
    with db:
        db.execute("UPDATE runs SET published=? WHERE status IN ('complete','partial') AND published IS NULL", (now(),))
    log("Published and verified " + publication["url"])


def run(db, state, should_publish, scheduled=False):
    # The process lock means any previous running row is from an interrupted job.
    with db:
        db.execute("UPDATE runs SET status='failed',finished=?,error='Interrupted before completion' WHERE status='running'", (now(),))
    if should_publish and db.execute("SELECT 1 FROM runs WHERE status IN ('complete','partial') AND published IS NULL").fetchone():
        log("Retrying unpublished completed report")
        publish(db)
    started = now()
    day = datetime.fromisoformat(started).astimezone(ZoneInfo("Europe/London")).date().isoformat()
    if scheduled and db.execute("SELECT 1 FROM runs WHERE day=? AND status IN ('complete','partial') AND published IS NOT NULL", (day,)).fetchone():
        log("Today's report is already published; no duplicate scheduled run needed")
        return None
    run_id = datetime.fromisoformat(started).strftime("%Y%m%dT%H%M%S%fZ")
    job = state / "runs" / run_id
    job.mkdir(parents=True, mode=0o700)
    with db:
        db.execute("INSERT INTO runs(id,started,day,status) VALUES (?,?,?,'running')", (run_id, started, day))
    sources = json.loads((ROOT / "sources.json").read_text())
    try:
        for name in ("sources.json", "criteria.md"):
            shutil.copyfile(ROOT / name, job / name)
        (job / "schema.json").write_text(json.dumps(SCHEMA, indent=2))
        history = context(db, sources)
        history.update({"run_id": run_id, "started": started, "report_day": day, "initial_pages": collect(sources, job)})
        current_inventory = inventory_urls(job)
        history["current_inventory_urls"] = sorted(current_inventory)
        history["new_inventory_urls"] = sorted(current_inventory - set(history["previous_inventory_urls"]))
        (job / "context.json").write_text(json.dumps(history, indent=2))
        log(f"Research started; private job directory: {job}")
        result = research(job)
        counts = ingest(db, result, job, sources, run_id, day, started)
        log(f"Research finished: {json.dumps(counts)}")
        if counts["status"] == "failed":
            raise RuntimeError("All sources failed; previous report retained")
    except Exception as exc:
        with db:
            db.execute("UPDATE runs SET status='failed',finished=?,error=? WHERE id=?", (now(), str(exc), run_id))
        raise
    enrich_photos(db, day=day)
    cache_photos(db)
    render(db, SITE)
    if should_publish:
        publish(db)
    return run_id


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="Import historical reports without publishing them as current listings")
    imp.add_argument("export", type=Path)
    runner = sub.add_parser("run")
    runner.add_argument("--publish", action="store_true")
    runner.add_argument("--scheduled", action="store_true", help="Skip research if today's report is already published")
    sub.add_parser("render")
    sub.add_parser("publish")
    sub.add_parser("status")
    args = parser.parse_args()
    args.state.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (args.state / "watch.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Another property-watch operation is running; skipping")
            return 0
        db = connect(args.state)
        try:
            if args.command == "import":
                log(f"Imported {import_export(db, args.export)} historical property hints")
            elif args.command == "run":
                run(db, args.state, args.publish, args.scheduled)
            elif args.command == "render":
                print(render(db, SITE))
            elif args.command == "publish":
                publish(db)
            else:
                for row in db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT 5"):
                    print(json.dumps(dict(row)))
                print("Historical hints:", db.execute("SELECT COUNT(*) FROM legacy").fetchone()[0])
                print("Known properties:", db.execute("SELECT COUNT(*) FROM properties").fetchone()[0])
        finally:
            db.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"FAILED: {exc}")
        raise SystemExit(1)
