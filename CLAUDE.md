# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (requires Python 3.11+)
pip install -r requirements.txt
playwright install chromium

# Run all enabled profiles (production)
python3 main.py run

# Run a single named profile, dry-run (no alerts, no marking seen)
python3 main.py run --profile "Lake Hartwell GA/SC" --dry-run

# Run with CLI overrides (all flags override the loaded profile for that run only)
python3 main.py run --max-piti 3500 --min-beds 3 --assumable-only --dry-run

# Show all profiles with affordability back-solve
python3 main.py list-profiles

# Verify alert credentials
python3 main.py test-alerts

# Start the FastAPI server (future web/iOS backend)
python3 main.py serve
```

**Important:** The system binary `python` resolves to Python 2.7 on this machine — always use `python3`.

## Architecture

The system has three layers that share the same core models:

```
main.py (CLI)  ←→  api/app.py (FastAPI)
                        ↓
                   app/engine.py          ← orchestrator
                  /     |     \
         scrapers/  filters/  alerts/
              ↓         ↓         ↓
         app/models.py  (all data flows through here)
```

**Data flow per run:**
1. `Engine.run()` iterates over active `SearchProfile` objects
2. For each profile, instantiates scrapers for its `sources` list
3. Each scraper yields `RawListing` objects via `AsyncIterator`
4. `evaluate_listing()` in `app/filters/listing_filter.py` applies ordered fail-fast checks and returns a `MatchResult` or `None`
5. New matches fire SMS + email alerts, then get written to `data/seen_listings.json`

### Key design invariants

**`SearchProfile` is the API contract.** It maps directly to a future `POST /api/v1/profiles` body. Every scraper, filter, and alert takes a `SearchProfile` — nothing hardcodes criteria. CLI `--flag` overrides work via `p.model_copy(update=overrides)` without mutating the JSON.

**`app/models.py` is the foundation.** All models are `pydantic.BaseModel` and JSON-serialisable. Adding a new filter field means: add it to `SearchProfile`, check it in `listing_filter.py`, expose it in `main.py`. No other files need changes.

**`app/engine.py` is the only place scrapers are instantiated.** The scraper registry is a plain dict — to add a new source, add a `DataSource` enum value, write the scraper, add it to the dict.

**Filter order in `evaluate_listing()` is intentional** (cheapest checks first, PITI calculation last). Assumable listings are surfaced even when PITI exceeds the market-rate budget — the filter passes them through with `HIGH` or `CRITICAL` priority.

### Scraper-specific notes

| Source | Mechanism | Residential IP required? |
|--------|-----------|--------------------------|
| Redfin | Playwright + DOM parsing (`.HomeCardContainer`) | No |
| Realtor | HomeHarvest library (wraps Realtor.com GraphQL) | No |
| Zillow | Playwright + `__NEXT_DATA__` JSON | Yes — blocked from cloud/VPS IPs |
| Homes.com | Playwright + `__NEXT_DATA__` + DOM fallback | Yes — CoStar WAF blocks servers |

Both Zillow and Homes.com emit a `WARNING` log and skip cleanly when blocked (no exception raised). Redfin has a known CloudFront concurrent-tab issue: the scraper collects all card data from the search page, **closes it**, then opens each detail page in a **fresh browser context**.

### Configuration

**Runtime credentials** → `.env` file (Twilio, SendGrid, headless flag, delay timings)  
**Search criteria** → `search_profiles.json` (all user-facing settings live here, not in `.env`)  
**Seen-listing deduplication** → `data/seen_listings.json` (written atomically; delete to re-process all listings)

New `TaxRegion` states: add to the `TAX_RATES` dict in `app/models.py` — no other changes needed.

### Alert priority logic

- `CRITICAL` — assumable loan detected AND PITI ≤ budget → immediate SMS + email per listing
- `HIGH` — assumable loan detected, PITI over budget at market rate → immediate SMS + email
- `NORMAL` — no assumable loan, PITI ≤ budget → batch digest only

### Scheduling

See `cron.md` for system cron, Docker Compose, and macOS launchd options. The Docker `cron` service uses a `sleep 21600` loop; replace the entrypoint with `supercronic` for a proper cron scheduler.
