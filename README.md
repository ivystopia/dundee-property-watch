# Dundee property watch

A daily Codex scheduled task produces the public report at https://ivystopia.github.io/dundee-property-watch/. The archive grows with each day's new properties, newest first, with 20 entries per page and horizontal Newer/Older navigation. Earlier entries never expire. Each property has original-agent and portal links, and a locally served thumbnail where available. Source diagnostics and research evidence remain private.

The watch covers Dundee and Broughty Ferry, exactly 2–3 bedrooms, and advertised asking/guide prices up to £270,000. `sources.json` contains all 24 discovery sources and their search instructions. `criteria.md` holds the current search and presentation rules. Existing homes, price changes and cross-portal duplicates are not reannounced. Older listings first discovered by this watch are labelled honestly; a first-seen date is not an original listing date.

## Runtime

- Python 3.11+ with Beautiful Soup and Pillow (`python3-bs4` and `python3-pil` on Debian), curl, Git and the authenticated GitHub CLI (`gh`) are required. Native execution uses the Codex desktop app and its subagents. The CLI is retained for the fallback backend and manual runs. Rendered-page research uses the existing browse-with-firefox skill/runtime.
- Three independent research workers each handle eight assigned sources concurrently, balancing portals, agents, auctioneers and builders. Each gets its own workspace, filtered source history, copied initial snapshots and private logs. Workers cannot delegate recursively. Their results and evidence are merged before central validation, database writes, deduplication and publishing; one failed worker does not discard another’s findings.
- The scheduled parent and researchers use GPT-6.1 Sol, high reasoning and Standard speed, with the research settings recorded in `settings.json` and each private run. Native workers start with fresh contexts and receive compact historical identities, source-specific cutoffs and prior routes; stale evidence quotes are omitted. CLI researchers retain their existing isolated configuration. Both use the ChatGPT plan; no API key or API billing is required.
- State is in `/home/ivy/.local/state/dundee-property-watch/`, with a SQLite database, process lock, durable native-run leases and private run directories. Short preparation and finalization stages hold the process lock; a lease prevents competing jobs between those stages. Interrupted leases expire and preserve earlier public reports. State and evidence are owner-only. Back up this directory to preserve deduplication history.
- Research receives prior property records, imported historical hints and per-source last-successful cutoffs. Failed or partial source checks do not advance those cutoffs. Use a minimum three-day overlap, wider after missed checks. A page fetch alone is never proof of complete inventory coverage.
- Per-source price coverage tracks the increase from £260,000 to £270,000. Until a source completes catch-up, research prioritizes all currently available homes in £260,001–£270,000, including older inventory. Previously reported homes still deduplicate normally; incomplete expansion coverage remains pending for later runs.
- Public-page evidence is saved with `fetch.py`. One optional SOCKS retry is available with `--tor`, using the optional `DUNDEE_TOR_PROXY` environment variable. Neither retrying nor a successful HTTP response proves a complete source check.
- Candidates require saved current evidence for address, price, bedrooms and availability. Unsupported candidates go to the private `rejected` table. The generated HTML escapes listing content and does not include raw evidence, prompts or source diagnostics.
- Thumbnail selection is a bounded HTTP step for newly added homes, with parallel fetching and cached success/failure. It prefers a usable main agent image, falling back to portal imagery, and rejects logos and tiny placeholders. Selected photos are downloaded once into the private SQLite cache, resized to at most 960 × 720, stripped of source metadata and published as JPEGs on GitHub Pages. Visitors make no third-party image requests. Failed downloads retry after one day; successful cached images remain available even if the source changes. Both discovery and downloading have a 60-second budget and at most four workers. New-build pictures are labelled as house-type illustrations. There is no daily model or browser pass for thumbnails.
- Generated files go under the ignored `site/` directory. Code, templates and the pinned Pico theme assets are tracked on `main`; only generated public files are pushed to `gh-pages`. The two branches have separate histories. Private state, evidence, source diagnostics and model logs are never copied into the publication tree.
- Publishing uses `pages.py` and the GitHub Pages branch configured in `publication-settings.json`. It builds a Git tree using an isolated index, so the current source branch, user-staged changes and worktree are preserved. Pushes are ordinary fast-forward pushes; unexpected remote files or concurrent changes fail safely. It waits for the exact commit to build, then checks every public page and asset byte-for-byte before marking publication successful. Existing publication retries continue without another research run. AWS is not involved.

## Site design and publication

The Pico Jade design was selected after screenshot comparisons with Just the Docs (the theme used by ivystopia.github.io) and Tabler; see [DESIGN.md](DESIGN.md). All 33 archive entries present during migration retained their exact visible card text, listing links and image URLs. The archive continues to grow without expiring older entries. The Dark mode toggle follows your device’s colour preference on the first visit and remembers an explicit choice across reloads and archive pages.

GitHub Pages publishes the root of `gh-pages`, using `.nojekyll`. A checksum manifest, `publication.json`, describes the public files, including content-addressed JPEG thumbnails under `images/`. The publisher rejects unexpected filenames and image checksum mismatches. The repository URL, source branch and public URL are explicit in `publication-settings.json`; the publisher verifies them against the current GitHub configuration before writing. Repository creation and Pages setup are one-time administrative steps, not actions performed by each daily run.

Scheduled publication creates **unsigned** commits through a command-scoped Git override; it never opens PGP pinentry or modifies the user’s signing settings. HTTPS pushes use the existing GitHub CLI authentication without storing a token in a URL or source file.

## Commands

From `/home/ivy/repos/personal/dundee-property-watch`:

```sh
python3 watch.py import /home/ivy/Downloads/ChatGPT-Dundee_Property_Watch.json
python3 watch.py run
python3 watch.py status
python3 watch.py render
python3 watch.py publish
python3 -m unittest discover -p 'test_*.py' -v
```

The import seeds deduplication hints; it does not present historical ChatGPT claims as verified current properties. A repeated import is idempotent. Candidate identity uses canonical URLs, agent references, full numbered addresses and individual plot identities. Central ingestion also compares each candidate against properties accepted earlier in the same run: independent workers cannot see each other's new discoveries. When postcodes or shared listing IDs are missing, a fallback requires the same full numbered address, locality, agent, bedrooms and house/unit category, with no conflicting known postcode or plot. Flats require an explicit unit; street-only and building-only flat addresses do not qualify. Unit letters and separators are preserved. Matches retain all listing aliases, prefer the original agent over portal fallbacks, and enrich the existing archive entry's links without changing its report date or historical price. Street-only legacy hints require an explicitly corroborated research match.

`run` performs a manual check and generates the local page. Add `--publish` to publish a completed run. Each of the three workers targets five minutes of research and has a ten-minute hard research limit. The whole job retains one process lock, and validates a saved checkpoint if the time limit is reached. Each worker writes its result to its own local JSON file instead of repeating a large result in the final model response. A research failure retains the previous report; a publication failure leaves a completed report pending for retry. Each candidate and source check can be inspected in the private database and per-run JSON files.

`run --research-backend cli` is the default. `run --research-backend native` prepares a leased job and returns its assignment manifest for a native Codex coordinator; the coordinator launches workers and calls `native_job.py finalize` as described below. The native entry point completes preparation without keeping a Python supervisor running during research.

## Schedule

The standalone local Codex automation runs daily at **08:00 Europe/London**, with a fresh chat for each run in Scheduled. Keep the computer on and the desktop app running. The existing local Personal project supplies the execution host; the saved prompt explicitly targets `/home/ivy/repos/personal/dundee-property-watch`. Read `scheduled-task.md` for the complete coordinator workflow.

The schedule and persistent state are independent of the setup conversation. Deleting that conversation does not remove the project automation, repository or SQLite history. Each Scheduled run verifies its own publication and reports its outcome there; no setup-chat heartbeat is required.

Native execution uses short durable stages rather than keeping a Python supervisor running while agents work:

```sh
python3 native_job.py prepare
python3 native_job.py start-worker /absolute/private/worker/job
python3 native_complete.py /absolute/private/worker/job
python3 native_job.py finalize /absolute/private/run/job
```

Codex launches three fresh research subagents between preparation and finalization. Each targets five minutes, has a ten-minute deadline and seals its result plus evidence before completion. The coordinator interrupts overdue workers and can finalize their saved checkpoints. Python validates and ingests the merged results, renders the permanent archive and publishes through the existing GitHub Pages publisher. Finalization is idempotent; publication retries never repeat ingestion. An already-published day skips research. The CLI fallback remains `python3 watch.py run --publish --scheduled`.

The parent allows one fatal-failure retry after 15 minutes within the 50-minute scheduled-run budget. A partial-source report is a successful outcome. If the app or computer was unavailable, the next actual run uses wider overlapping lookbacks and unchanged incomplete-source cutoffs; immediate catch-up on reopening the app is not promised.

Pause or run the task through Codex Scheduled. The former systemd timer and installed units are retained disabled for rollback, with timestamped backups beside the originals. Re-enable the old timer only after pausing the Codex automation to prevent duplicate scheduling. The process lock and native lease also guard overlapping manual invocations.

`fetch.py` still reads the optional private proxy configuration at `/home/ivy/.config/dundee-property-watch.env`, so it no longer depends on a service loading that file. Explicit `DUNDEE_TOR_PROXY` values override it. Machine-specific configuration is never placed in the repository.

Architecture benchmarks use `benchmark.py` and isolated databases and output below the private state directory. They cannot publish pages or write to production history. Matched trials share pre-run history and entry snapshots; follow-up pages remain live. Account allowance percentages are the usage measure, with token/cache totals only supporting diagnostics. API or purchased-credit rates are not conversions for included-plan allowance.

The 6 October architecture comparison selected native research: all three native trials retained the nine pooled verified discoveries, while CLI trials retained eight, eight and five. Each complete trial showed one percentage point of weekly allowance use. The global integer meter and concurrent coordination make the precise usage comparison inconclusive; the user's preference for native when quality holds decided the cutover. The full comparison and evidence remain in the private state directory.

## Model and reasoning

Edit `/home/ivy/repos/personal/dundee-property-watch/settings.json` to choose researcher model, reasoning effort and concurrency for subsequent runs. The current setting is `gpt-6.1-sol` with `high` effort. Keep the automation's parent model and effort consistent when changing researcher settings. `max_concurrent_research` defaults to 3; 1 or 2 runs the source groups sequentially. Increasing effort cannot fix blocked websites or missing listing dates. Each run saves its selected settings privately beside its evidence. The 2 October model benchmark selected Sol high; the architecture benchmark separately compares CLI and native orchestration using actual account allowance observations.
