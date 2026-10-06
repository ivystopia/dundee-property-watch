"""Native stages, durable leases, sealing and duplicate prevention."""
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from native_complete import complete
from native_job import prepare, start_worker, merge_assignments, finalize, locked_state, active_lease, recover_leases
from watch import atomic_json
import watch


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.site = self.root / "site"

    def tearDown(self):
        self.temp.cleanup()

    def prepared(self):
        with patch("watch.collect", return_value={}):
            result = prepare(self.state, self.site, should_publish=False)
        self.job = Path(result["job"])
        self.manifest = json.loads(Path(result["manifest"]).read_text())
        return [Path(w["job"]) for w in self.manifest["workers"]]

    def finish_workers(self, workers):
        for worker in workers:
            start_worker(worker)
            sources = json.loads((worker / "sources.json").read_text())
            atomic_json(worker / "research-result.json", {"candidates": [], "source_checks": [
                {"source_id": s["id"], "status": "partial", "detail": "Both localities inspected, incomplete pagination", "urls": []} for s in sources]})
            complete(worker)

    def test_sealed_completion_ignores_late_checkpoint_changes(self):
        workers = self.prepared()
        self.finish_workers(workers)
        for worker in workers:
            atomic_json(worker / "research-result.json", {"candidates": [], "source_checks": []})
        result = merge_assignments(self.job)
        self.assertEqual(len(result["source_checks"]), 24)
        self.assertEqual(merge_assignments(self.job), result)
        for worker in workers:
            with self.assertRaises(ValueError):
                complete(worker)

    def test_live_workers_cannot_be_finalized_accidentally(self):
        workers = self.prepared()
        start_worker(workers[0])
        with self.assertRaisesRegex(ValueError, "Worker still active"):
            merge_assignments(self.job)
        self.assertFalse((workers[0] / "native-closed.json").exists())

    def test_checkpoint_recovery_preserves_other_worker_results(self):
        workers = self.prepared()
        self.finish_workers(workers[:2])
        start_worker(workers[2])
        result = merge_assignments(self.job, allow_checkpoints=True)
        self.assertEqual(len(result["source_checks"]), 24)
        self.assertEqual(sum(c["status"] == "failed" for c in result["source_checks"]), 8)

    def test_foreign_completion_is_rejected_only_for_its_worker(self):
        workers = self.prepared()
        self.finish_workers(workers)
        atomic_json(workers[0] / "native-complete.json", {"job_id": "wrong", "finished_unix": time.time()})
        result = merge_assignments(self.job)
        self.assertEqual(sum(c["status"] == "failed" for c in result["source_checks"]), 8)

    def test_expired_worker_recovers_valid_checkpoint_and_refuses_late_completion(self):
        workers = self.prepared()
        self.finish_workers(workers[:2])
        worker = workers[2]
        assignment = start_worker(worker)
        assignment["deadline_unix"] = 0
        atomic_json(worker / "native-assignment.json", assignment)
        sources = json.loads((worker / "sources.json").read_text())
        atomic_json(worker / "research-result.json", {"candidates": [], "source_checks": [
            {"source_id": s["id"], "status": "partial", "detail": "Saved inventory before timeout", "urls": []} for s in sources]})
        with self.assertRaisesRegex(ValueError, "past its deadline"):
            complete(worker)
        result = merge_assignments(self.job)
        self.assertEqual(len(result["source_checks"]), 24)
        self.assertTrue(all(c["status"] == "partial" for c in result["source_checks"]))
        atomic_json(worker / "research-result.json", {"candidates": [], "source_checks": []})
        self.assertEqual(merge_assignments(self.job), result)

    def test_malformed_failed_worker_does_not_discard_sealed_workers(self):
        workers = self.prepared()
        self.finish_workers(workers[:2])
        start_worker(workers[2])
        (workers[2] / "research-result.json").write_text("incomplete JSON")
        result = merge_assignments(self.job, allow_checkpoints=True)
        self.assertEqual(len(result["source_checks"]), 24)
        self.assertEqual(sum(c["status"] == "failed" for c in result["source_checks"]), 8)
        self.assertEqual(sum(c["status"] == "partial" for c in result["source_checks"]), 16)

    def test_native_lease_prevents_overlapping_preparation_and_cli_run(self):
        self.prepared()
        with patch("watch.collect") as collection:
            self.assertEqual(prepare(self.state, self.site, should_publish=False), {"status": "busy"})
            collection.assert_not_called()
        result = subprocess.run([sys.executable, str(Path(__file__).parent / "watch.py"), "--state", str(self.state), "--site", str(self.site), "run", "--scheduled"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("active lease", result.stdout)

    def test_expired_lease_is_recovered_and_closes_workers(self):
        workers = self.prepared()
        with locked_state(self.state) as db:
            db.execute("UPDATE native_leases SET deadline=0")
            db.commit()
            recover_leases(db)
            self.assertIsNone(active_lease(db))
            self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0], "failed")
        self.assertTrue(all((w / "native-closed.json").exists() for w in workers))

    def test_finalization_is_idempotent_and_publication_retry_does_not_reingest(self):
        workers = self.prepared()
        self.finish_workers(workers)
        self.manifest["should_publish"] = True
        atomic_json(self.job / "native-research.json", self.manifest)
        with patch("watch.enrich_photos"), patch("watch.cache_photos"), patch("watch.publish", side_effect=RuntimeError("Publication failed")):
            with self.assertRaisesRegex(RuntimeError, "Publication failed"):
                finalize(self.job)
        with locked_state(self.state) as db:
            self.assertEqual(db.execute("SELECT status FROM native_leases").fetchone()[0], "finalized")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_checks").fetchone()[0], 24)
        def published(db):
            with db:
                db.execute("UPDATE runs SET published='verified' WHERE status='partial'")
        with patch("watch.publish", side_effect=published) as publishing:
            first = finalize(self.job)
            second = finalize(self.job)
        self.assertEqual(first, second)
        publishing.assert_called_once()
        with patch("watch.collect") as collection:
            self.assertEqual(prepare(self.state, self.site, should_publish=False)["status"], "already_published")
            collection.assert_not_called()

    def test_contended_process_lock_does_not_open_database(self):
        self.state.mkdir()
        with (self.state / "watch.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, str(Path(__file__).parent / "watch.py"), "--state", str(self.state), "run", "--scheduled"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.state / "history.sqlite3").exists())

    def test_native_preparation_reports_busy_for_contended_process_lock(self):
        self.state.mkdir()
        with (self.state / "watch.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(prepare(self.state, self.site), {"status": "busy"})
        self.assertFalse((self.state / "history.sqlite3").exists())

    def test_native_run_entry_point_prepares_without_launching_cli(self):
        argv = ["watch.py", "--state", str(self.state), "--site", str(self.site), "run", "--research-backend", "native", "--scheduled"]
        with patch.object(sys, "argv", argv), patch("native_job.prepare", return_value={"status": "busy"}) as preparation, patch("watch.run") as cli:
            self.assertEqual(watch.main(), 0)
        preparation.assert_called_once_with(self.state, self.site, scheduled=True, should_publish=False, overall_timeout_seconds=3000)
        cli.assert_not_called()
        self.assertFalse((self.state / "history.sqlite3").exists())

    def test_all_failed_sources_leave_previous_public_report_intact(self):
        workers = self.prepared()
        for worker in workers:
            start_worker(worker)
            complete(worker)
        self.site.mkdir()
        page = self.site / "index.html"
        page.write_text("Previous verified report")
        with patch("watch.publish") as publication, patch("watch.render") as rendering:
            with self.assertRaisesRegex(RuntimeError, "All sources failed"):
                finalize(self.job)
        publication.assert_not_called()
        rendering.assert_not_called()
        self.assertEqual(page.read_text(), "Previous verified report")
        with locked_state(self.state) as db:
            self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0], "failed")
            self.assertEqual(db.execute("SELECT status FROM native_leases").fetchone()[0], "failed")
