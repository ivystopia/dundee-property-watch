# Scheduled Dundee Property Watch

Run daily at 08:00 Europe/London as a standalone local Codex scheduled task, starting a fresh chat each time. Keep the computer and desktop app running. Use GPT-6.1 Sol, high reasoning and Standard speed for the parent and all researchers, regardless of interactive defaults.

The selected backend is recorded in the saved automation prompt. Both backends preserve the existing private history and public webpage. Python alone validates evidence, deduplicates homes, writes history, caches thumbnails and publishes GitHub Pages.

The automation belongs to the Personal project and operates independently of the setup conversation. Its state lives in this repository and the private state directory. Do not create verification heartbeats or follow-ups tied to the setup chat. Complete waiting, retry and publication verification within each standalone Scheduled run; report the first unattended run's verified outcome in its own Scheduled chat.

## Native workflow

1. From `/home/ivy/repos/personal/dundee-property-watch`, run `/usr/bin/python3 native_job.py prepare`. This short stage retries any pending publication, skips an already-published day, or returns a private job and manifest path. A `busy` result means another run owns the state; exit without duplicating work. The lease lasts at most 50 minutes and survives separate tool calls. No Python supervisor waits during native research.
2. Read the returned `native-research.json` manifest. Launch the prepared source groups using native subagents with fresh contexts, at most `max_concurrent_research` concurrently. Immediately before launching each worker, run `/usr/bin/python3 native_job.py start-worker WORKER_JOB` and retain its returned deadline. Give the worker its `prompt.txt` path and job path; tell it to read that prompt and the scoped files, that other workers own other directories, and that it must not revert their edits or delegate. Explicitly choose `gpt-6.1-sol` with `high` reasoning. Workers use the existing public-web evidence helper and Firefox skill when rendering is required.
3. Each worker writes atomic checkpoints, then runs `/usr/bin/python3 /home/ivy/repos/personal/dundee-property-watch/native_complete.py WORKER_JOB`. This seals its evidence and result before its deadline. Track each launched worker once. Wait for completion notifications in intervals no longer than 60 seconds. Interrupt a worker at its ten-minute deadline or when its job is closed. Respect lower configured concurrency by launching remaining groups only when slots free.
4. After workers finish, run `/usr/bin/python3 native_job.py finalize JOB`. If a worker failed or was interrupted, interrupt any remaining worker before using `finalize JOB --checkpoints` to recover partial checkpoints. This short stage merges immutable evidence, validates and ingests the results, renders the archive and publishes through the existing verified publisher. Retrying finalization never reingests a completed run.

The scheduled parent does not read the full property history or research listings itself; only workers read their scoped context. Keep tool output compact. Never write to the database or publication branch directly.

Preparation, evidence retrieval and publication need public network access; the publisher also uses the existing GitHub CLI login. If a tool sandbox explicitly blocks the authorized command, rerun that stage with host escalation and a precise justification. Never redirect or copy authentication state to work around a blocked command. A remote website's HTTP 403 is a source limitation, not a reason for repeated escalation or retries.

## CLI fallback

Run `/usr/bin/python3 /home/ivy/repos/personal/dundee-property-watch/watch.py run --publish --scheduled` from the repository and wait for its exit status. This starts the three existing isolated CLI researchers. The command rejects a concurrent native lease and has a 50-minute overall deadline.

## Failure handling and reporting

Keep the entire scheduled run within 50 minutes. If research fails fatally, wait 15 minutes and retry preparation once only when sufficient time remains. The native job lease deadline and worker deadlines still apply. If publication alone fails, retry `finalize JOB` once after 15 minutes within that deadline; it publishes the existing completed report without another research run. Wait in intervals no longer than 60 seconds. A partial-source report is successful and does not trigger a retry. An expired interrupted lease is marked failed by the next preparation, leaving the earlier public report intact.

Report the actual outcome, new-property count and verified public report link. Surface fatal errors and required user action in Scheduled. Keep source diagnostics and evidence private; do not claim full coverage for partial reports. Missed days use the existing overlapping lookback and per-source cutoff recovery on the next actual run; immediate catch-up on opening the app is not guaranteed.
