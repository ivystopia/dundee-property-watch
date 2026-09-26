# Dundee property watch

A local daily research job produces the public report at https://ivystopia.github.io/dundee-property-watch/. The archive grows with each day's new properties, newest first, with 20 entries per page and horizontal Newer/Older navigation. Earlier entries never expire. Each property has original-agent and portal links, and a hotlinked thumbnail where available. Source diagnostics and research evidence remain private.

The watch covers Dundee and Broughty Ferry, exactly 2–3 bedrooms, and advertised asking/guide prices up to £260,000. `sources.json` contains all 24 discovery sources and their search instructions. `criteria.md` holds the current search and presentation rules. Existing homes, price changes and cross-portal duplicates are not reannounced. Older listings first discovered by this watch are labelled honestly; a first-seen date is not an original listing date.

## Runtime

- Python 3.11+ with Beautiful Soup and Pillow (`python3-bs4` and `python3-pil` on Debian), curl, Git, the authenticated GitHub CLI (`gh`) and the authenticated Codex CLI are required. Optional rendered-page research uses the existing browse-with-firefox skill/runtime.
- Three independent research workers each handle eight assigned sources concurrently, balancing portals, agents, auctioneers and builders. Each gets its own workspace, filtered source history, copied initial snapshots and private logs. Workers cannot delegate recursively. Their results and evidence are merged before central validation, database writes, deduplication and publishing; one failed worker does not discard another’s findings.
- The researcher uses the model and reasoning level from `settings.json` (initially GPT-6 Astra, high), live web search and a workspace-write sandbox with network access. It uses the existing ChatGPT login, not a separately billed API key. It does not inherit general user MCP/plugin configuration or arbitrary credential environment variables.
- State is in `/home/ivy/.local/state/dundee-property-watch/`, with a SQLite database, process lock and private run directories. The state directory and files are owner-only. Back up this directory to preserve deduplication history.
- Research receives prior property records, imported historical hints and per-source last-successful cutoffs. Failed or partial source checks do not advance those cutoffs. Use a minimum three-day overlap, wider after missed checks. A page fetch alone is never proof of complete inventory coverage.
- Public-page evidence is saved with `fetch.py`. One optional SOCKS retry is available with `--tor`, using the optional `DUNDEE_TOR_PROXY` environment variable. Neither retrying nor a successful HTTP response proves a complete source check.
- Candidates require saved current evidence for address, price, bedrooms and availability. Unsupported candidates go to the private `rejected` table. The generated HTML escapes listing content and does not include raw evidence, prompts or source diagnostics.
- Thumbnail selection is a bounded HTTP step for newly added homes, with parallel fetching and cached success/failure. It prefers a usable main agent image, falling back to portal imagery, and rejects logos and tiny placeholders. Images stay on their original hosts. Broken images hide automatically; new-build pictures are labelled as house-type illustrations. There is no daily model or browser pass for thumbnails.
- Generated files go under the ignored `site/` directory. Code, templates and the pinned Pico theme assets are tracked on `main`; only generated public files are pushed to `gh-pages`. The two branches have separate histories. Private state, evidence, source diagnostics and model logs are never copied into the publication tree.
- Publishing uses `pages.py` and the GitHub Pages branch configured in `publication-settings.json`. It builds a Git tree using an isolated index, so the current source branch, user-staged changes and worktree are preserved. Pushes are ordinary fast-forward pushes; unexpected remote files or concurrent changes fail safely. It waits for the exact commit to build, then checks every public page and asset byte-for-byte before marking publication successful. Existing publication retries continue without another research run. AWS is not involved.

## Site design and publication

The light Pico Jade design was selected after screenshot comparisons with Just the Docs (the theme used by ivystopia.github.io) and Tabler; see [DESIGN.md](DESIGN.md). All 33 archive entries present during migration retained their exact visible card text, listing links and image URLs. The archive continues to grow without expiring older entries.

GitHub Pages publishes the root of `gh-pages`, using `.nojekyll`. A checksum manifest, `publication.json`, describes the public files. The repository URL, source branch and public URL are explicit in `publication-settings.json`; the publisher verifies them against the current GitHub configuration before writing. Repository creation and Pages setup are one-time administrative steps, not actions performed by each daily run.

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

## Schedule

After validating a manual run and publication:

```sh
python3 install_timer.py
systemctl --user status dundee-property-watch.timer
journalctl --user -u dundee-property-watch.service
```

The service loads optional machine-specific values from `~/.config/dundee-property-watch.env`. The existing SOCKS retry endpoint is retained there, outside the public repository. Manual research/helper calls read the same configuration automatically; an explicit `DUNDEE_TOR_PROXY` in the shell overrides it. No secret or private host configuration is embedded in the public code.

The user timer runs at 08:00 Europe/London, automatically following BST/GMT. `Persistent=true` catches up when the user service manager starts after downtime; a sleeping machine checks after resume. It does not wake a powered-off machine or start before the user's service manager is available. The scheduled command skips a day already successfully published, including after a publication retry. A failed service retries once after 15 minutes within its start-limit window.

No Codex desktop app or open terminal is required: systemd starts three fresh, concurrent `codex exec` processes for each research run. This is independent of the existing phone remote-control service. Ivy's account already has systemd lingering enabled, so its user services can start at boot and continue after logout.

Pause with `systemctl --user disable --now dundee-property-watch.timer`. This does not interrupt a current run. Stop an active run with `systemctl --user stop dundee-property-watch.service`; interrupted research is marked failed on the next run.

The previous ChatGPT watch is intentionally left in place for a short comparison period. Its manual results from 16 September 2026 are included in the imported baseline. Remove the old watch separately after comparing the outputs.

## Model and reasoning

Edit `/home/ivy/repos/personal/dundee-property-watch/settings.json` to choose the model and `reasoning_effort` for subsequent runs. No service reload is needed. `max_concurrent_research` defaults to 3; set it to 1 or 2 to reduce simultaneous usage, at the cost of running some of the three source groups sequentially. The initial setting is `gpt-6-astra` with `high` effort, independent of interactive CLI preferences. Astra's documented levels are `low`, `medium`, `high`, `xhigh` and `max`; see https://developers.openai.com/api/docs/models/gpt-6-astra. `high` is the initial choice for multi-source research and ambiguous identities. `medium` is a useful later comparison if runtime or usage becomes a concern. Increasing effort cannot fix blocked websites or missing listing dates. Each run saves its selected settings privately beside its evidence.
