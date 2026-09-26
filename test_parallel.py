"""Offline checks for isolated, bounded research and fault-tolerant merging."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from watch import merge_worker, partition_sources, research


class ParallelResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.job = Path(self.temp.name)
        (self.job / "evidence").mkdir()
        self.sources = [{"id": f"source-{index:02d}", "urls": [f"https://example.com/{index}"]} for index in range(24)]
        self.history = {"previous_properties": [{"address": "known home"}], "legacy_reports": [{"id": "legacy"}],
                        "previous_inventory_urls": {"https://example.com/property/old": "2026-09-15"},
                        "current_inventory_urls": ["https://example.com/property/new"],
                        "new_inventory_urls": ["https://example.com/property/new"],
                        "initial_pages": {}, "last_successful_check": {}, "previous_source_checks": {}}
        for source in self.sources:
            name = source["id"]
            path = f"evidence/{name}.json"
            (self.job / path).write_text(json.dumps({"url": source["urls"][0], "status": 200}))
            self.history["initial_pages"][name] = [{"file": path, "url": source["urls"][0]}]
            self.history["last_successful_check"][name] = "2026-09-15"
            self.history["previous_source_checks"][name] = {"status": "partial", "detail": name}
        for name, value in [("sources.json", self.sources), ("context.json", self.history), ("schema.json", {})]:
            (self.job / name).write_text(json.dumps(value))
        (self.job / "criteria.md").write_text("Assigned property criteria")

    def tearDown(self):
        self.temp.cleanup()

    def worker_result(self, worker):
        sources = json.loads((worker / "sources.json").read_text())
        (worker / "evidence/property.json").write_text(json.dumps({"worker": worker.name}))
        return {"source_checks": [{"source_id": source["id"], "status": "partial", "detail": "Checked current inventory", "urls": source["urls"]} for source in sources],
                "candidates": [{"source_ids": [sources[0]["id"]], "evidence": [{"file": "evidence/property.json", "field": "address", "quote": "Example home"}]}]}

    def test_balanced_complete_disjoint_partition(self):
        groups = partition_sources(self.sources)
        self.assertEqual([len(group) for group in groups], [8, 8, 8])
        actual = [source["id"] for group in groups for source in group]
        self.assertEqual(len(actual), len(set(actual)))
        self.assertEqual(set(actual), {source["id"] for source in self.sources})
        with self.assertRaises(ValueError):
            partition_sources(self.sources + [self.sources[0]])

    def test_concurrent_workers_have_scoped_context_and_distinct_evidence(self):
        barrier = threading.Barrier(3)
        def fake(worker, settings):
            self.assertEqual(settings["research_budget_minutes"], 5)
            self.assertEqual(settings["research_timeout_seconds"], 600)
            sources = json.loads((worker / "sources.json").read_text())
            assigned = {source["id"] for source in sources}
            context = json.loads((worker / "context.json").read_text())
            for key in ("initial_pages", "last_successful_check", "previous_source_checks"):
                self.assertEqual(set(context[key]), assigned)
            for key in ("previous_properties", "legacy_reports", "previous_inventory_urls", "current_inventory_urls", "new_inventory_urls"):
                self.assertEqual(context[key], self.history[key])
            self.assertEqual(len(list((worker / "evidence").glob("*.json"))), 8)
            barrier.wait(timeout=5)  # Cannot pass unless all three calls overlap.
            return self.worker_result(worker)
        with patch("watch.research_worker", side_effect=fake) as calls:
            result = research(self.job)
        self.assertEqual(calls.call_count, 3)
        self.assertEqual(len(result["source_checks"]), 24)
        self.assertEqual(len(result["candidates"]), 3)
        paths = {candidate["evidence"][0]["file"] for candidate in result["candidates"]}
        self.assertEqual(paths, {f"evidence/worker-{index}/property.json" for index in range(1, 4)})
        for path in paths:
            snapshot = json.loads((self.job / path).read_text())
            self.assertEqual(snapshot["worker"], Path(path).parent.name)
        self.assertEqual(json.loads((self.job / "research-result.json").read_text()), result)

    def test_worker_failure_preserves_other_results_and_all_source_checks(self):
        def fake(worker, settings):
            if worker.name == "worker-2":
                raise RuntimeError("Simulated rate limit")
            return self.worker_result(worker)
        with patch("watch.research_worker", side_effect=fake):
            result = research(self.job)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(len(result["source_checks"]), 24)
        failed = {check["source_id"] for check in result["source_checks"] if check["status"] == "failed"}
        self.assertEqual(failed, {source["id"] for source in self.sources[1::3]})

    def test_lower_concurrency_keeps_all_three_source_groups(self):
        configuration = self.job / "config"
        configuration.mkdir()
        (configuration / "settings.json").write_text(json.dumps({"max_concurrent_research": 1}))
        thread_ids = set()
        def fake(worker, settings):
            thread_ids.add(threading.get_ident())
            return self.worker_result(worker)
        with patch("watch.ROOT", configuration), patch("watch.research_worker", side_effect=fake) as calls:
            result = research(self.job)
        self.assertEqual(calls.call_count, 3)
        self.assertEqual(len(thread_ids), 1)
        self.assertEqual(len(result["source_checks"]), 24)

    def test_unassigned_sources_and_escaping_evidence_are_rejected(self):
        with patch("watch.research_worker", side_effect=lambda worker, config: self.worker_result(worker)):
            research(self.job)
        worker = self.job / "workers/worker-1"
        assigned = self.sources[0::3]
        good = self.worker_result(worker)
        bad = deepcopy(good)
        bad["candidates"][0]["source_ids"] = [self.sources[1]["id"]]
        with self.assertRaises(ValueError):
            merge_worker(self.job, worker, assigned, bad)
        bad = deepcopy(good)
        bad["source_checks"].append(deepcopy(bad["source_checks"][0]))
        with self.assertRaises(ValueError):
            merge_worker(self.job, worker, assigned, bad)
        bad = deepcopy(good)
        bad["candidates"][0]["evidence"][0]["file"] = "context.json"
        with self.assertRaises(ValueError):
            merge_worker(self.job, worker, assigned, bad)


if __name__ == "__main__":
    unittest.main()
