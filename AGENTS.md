# Dundee Property Watch

This repository owns the daily Dundee and Broughty Ferry property watch. Read `criteria.md`, `sources.json`, `research.md` and `README.md` before changing behavior. The current cap is £270,000, with exactly two or three bedrooms and currently available residential sales.

The persistent state, evidence and benchmark results under `/home/ivy/.local/state/dundee-property-watch` are private. Never commit or publish them. Python owns validation, SQLite history, deduplication, thumbnails and GitHub Pages publication. Research workers own only their assigned private directories and must not edit the repository, read credentials, publish, or delegate recursively.

Scheduled runs must follow `scheduled-task.md`. Use GPT-6.1 Sol, high reasoning, Standard speed for the parent and researchers. Native workers start with fresh contexts, preserve the eight-source assignments and use the generated prompt and completion helper. Codex owns orchestration. Short Python preparation/finalization stages use a durable lease to prevent overlapping runs; no background Python supervisor is required for native research.

Keep the legacy CLI backend available for manual runs and rollback. Use isolated state and output for benchmarks; never invoke production publication during a benchmark. Existing historical entries, evidence requirements and partial-source cutoffs must survive refactors. Run the offline unittest suite after changing execution or persistence.
