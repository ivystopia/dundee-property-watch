"""Benchmark isolation, matched settings and recovery without network/model calls."""
from contextlib import closing, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import benchmark
from model import SCHEMA
from native_complete import complete
from native_job import start_worker
from store import connect
from watch import atomic_json


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.benchmarks = self.root / "benchmarks"
        self.benchmarks.mkdir(mode=0o700)
        self.patch = patch.object(benchmark, "BENCHMARK_ROOT", self.benchmarks)
        self.patch.start()
        self.output = redirect_stdout(StringIO())
        self.output.__enter__()
        self.baseline = self.root / "baseline"
        (self.baseline / "evidence").mkdir(parents=True)
        self.history = {"started": "2026-10-27T08:00:00+00:00", "report_day": "2026-10-27", "initial_pages": {},
                        "previous_properties": [], "legacy_reports": [], "previous_inventory_urls": {},
                        "last_successful_check": {}, "previous_source_checks": {}, "price_expansions": {}}
        atomic_json(self.baseline / "context.json", self.history)
        atomic_json(self.baseline / "schema.json", SCHEMA)
        self.sources = json.loads((benchmark.watch.ROOT / "sources.json").read_text())
        atomic_json(self.baseline / "sources.json", self.sources)
        self.destination = self.benchmarks / "comparison"

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.patch.stop()
        self.tmp.cleanup()

    def prepare(self, **kwargs):
        return benchmark.prepare(self.destination, self.baseline, **kwargs)

    def test_production_and_root_paths_rejected_before_creation(self):
        for path in (self.root / "production", self.benchmarks, self.benchmarks / ".." / "production"):
            with self.assertRaises(ValueError):
                benchmark.prepare(path, self.baseline)
        self.assertFalse((self.root / "production").exists())

    def test_symlink_escape_rejected_before_creation(self):
        (self.benchmarks / "escape").symlink_to(self.baseline, target_is_directory=True)
        with self.assertRaises(ValueError):
            benchmark.prepare(self.benchmarks / "escape" / "trial", self.baseline)
        self.assertFalse((self.baseline / "trial").exists())

    def test_symlink_or_hardlink_output_rejected_before_ingestion(self):
        self.prepare()
        job = benchmark.setup_trial(self.destination, 1, "cli")
        outside = self.root / "outside"
        outside.mkdir()
        (job / "site").symlink_to(outside, target_is_directory=True)
        with patch.object(benchmark, "ingest") as ingest:
            with self.assertRaises(ValueError):
                benchmark.finish_trial(job)
            ingest.assert_not_called()
        (job / "site").unlink()
        target = outside / "important"
        target.write_text("keep")
        (job / "summary.json").hardlink_to(target)
        with self.assertRaises(ValueError):
            benchmark.finish_trial(job)
        self.assertEqual(target.read_text(), "keep")

    def test_explicit_models_preserve_matched_effort_and_old_names(self):
        self.prepare()
        old = benchmark.setup_trial(self.destination, 1, "cli")
        self.assertEqual(old.name, "pair-01-cli")
        arm = benchmark.setup_trial(self.destination, 1, "native", model="gpt-6.1-terra", coordinator_model="gpt-6.1-sol")
        config = json.loads((arm / "settings.json").read_text())
        self.assertIn("gpt-6.1-terra-coordinator-gpt-6.1-sol", arm.name)
        self.assertEqual(config["model"], "gpt-6.1-terra")
        self.assertEqual(config["reasoning_effort"], "high")
        self.assertEqual(config["coordinator_reasoning_effort"], "xhigh")
        self.assertEqual(config["service_tier"], "default")
        for arguments in ({"model": "wrong"}, {"coordinator_effort": "high"}, {"coordinator_model": "wrong"}):
            with self.assertRaises(ValueError):
                benchmark.setup_trial(self.destination, 2, "native", **arguments)

    def test_changed_fixture_or_code_is_rejected(self):
        self.prepare()
        with patch.object(benchmark.watch, "ROOT", self.root):
            with self.assertRaises((ValueError, FileNotFoundError)):
                benchmark.setup_trial(self.destination, 1, "native")
        (self.destination / "fixture/context.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "fixture changed"):
            benchmark.setup_trial(self.destination, 1, "native")

    def test_sibling_traversal_and_extra_fixture_file_are_rejected(self):
        self.prepare()
        with self.assertRaises(ValueError):
            benchmark.private_path(self.destination / "fixture" / ".." / "sibling")
        (self.destination / "fixture" / "history.sqlite3").write_text("unverified database")
        with self.assertRaisesRegex(ValueError, "file set changed"):
            benchmark.setup_trial(self.destination, 1, "native")

    def test_backup_preserves_wal_and_all_tables_without_changing_source(self):
        state = self.root / "production"
        with closing(connect(state)) as original:
            original.execute("CREATE TABLE private_identity_test (value TEXT)")
            original.execute("INSERT INTO private_identity_test VALUES ('committed in WAL')")
            original.commit()
            self.prepare(history_db=state / "history.sqlite3")
            job = benchmark.setup_trial(self.destination, 1, "cli")
            with closing(sqlite3.connect(job / "validation-state/history.sqlite3")) as trial:
                self.assertEqual(trial.execute("SELECT value FROM private_identity_test").fetchone()[0], "committed in WAL")
            self.assertEqual(original.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            self.assertIn("property_descriptions", {r[0] for r in original.execute("SELECT name FROM sqlite_master")})

    def test_finish_excludes_existing_reports_on_same_day_and_never_publishes(self):
        state = self.root / "production"
        with closing(connect(state)) as original, original:
            original.execute("INSERT INTO properties VALUES ('old','{}',?,?)", (self.history["started"], self.history["started"]))
            original.execute("INSERT INTO reports VALUES (?,'old','{\"address\":\"old\"}')", (self.history["report_day"],))
        self.prepare(history_db=state / "history.sqlite3")
        job = benchmark.setup_trial(self.destination, 1, "cli")
        atomic_json(job / "research-result.json", {"candidates": [], "source_checks": [
            {"source_id": s["id"], "status": "partial", "detail": "Fixture only", "urls": []} for s in self.sources]})
        observed = {"coordinator": {"model": "gpt-6.1-sol", "reasoning_effort": "xhigh", "evidence": "runtime record"}}
        atomic_json(job / "execution.json", observed)
        with patch.object(benchmark, "render") as render, patch("watch.publish_site") as publish:
            summary = benchmark.finish_trial(job)
            publish.assert_not_called()
            self.assertEqual(render.call_args.args[1], job / "site")
        self.assertEqual(summary["accepted"], [])
        self.assertFalse(summary["published"])
        self.assertEqual(summary["observed_execution"], observed)
        with self.assertRaisesRegex(ValueError, "already finalized"):
            benchmark.finish_trial(job)

    def test_native_checkpoint_recovery_keeps_sealed_workers(self):
        self.prepare()
        job = benchmark.trial(self.destination, 1, "native", model="gpt-6.1-terra")
        manifest = json.loads((job / "native-research.json").read_text())
        for slot in manifest["workers"][:2]:
            worker = Path(slot["job"])
            start_worker(worker)
            sources = json.loads((worker / "sources.json").read_text())
            atomic_json(worker / "research-result.json", {"candidates": [], "source_checks": [
                {"source_id": s["id"], "status": "partial", "detail": "Fixture check", "urls": []} for s in sources]})
            complete(worker)
        with patch("watch.publish_site") as publish:
            summary = benchmark.finish_trial(job, checkpoints=True)
            publish.assert_not_called()
        self.assertEqual(summary["source_status"], {"partial": 16, "failed": 8})
        self.assertIsNone(summary["observed_execution"])

    def test_native_worker_manifest_cannot_escape_trial(self):
        self.prepare()
        job = benchmark.trial(self.destination, 1, "native")
        path = job / "native-research.json"
        manifest = json.loads(path.read_text())
        manifest["workers"][0]["job"] = str(self.benchmarks / "another-trial" / "workers" / "worker-1")
        atomic_json(path, manifest)
        with self.assertRaisesRegex(ValueError, "escapes isolated trial"):
            benchmark.finish_trial(job, checkpoints=True)


if __name__ == "__main__":
    unittest.main()
