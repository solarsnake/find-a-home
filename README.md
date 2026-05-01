# find-a-home

Real estate deal finder with PITI calculation and assumable-loan detection. Scrapes Redfin, Realtor.com, Zillow, and Homes.com against configurable search profiles, filters by budget and criteria, and sends SMS + email alerts for matches.

## Requirements

- Python 3.11+
- A residential IP for Zillow and Homes.com (both block cloud/VPS addresses)

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # fill in Twilio/SendGrid credentials
```

Configure your search criteria in `search_profiles.json` (see the existing file for examples).

## Usage

```bash
# Run all enabled profiles
python3 main.py run

# Run a single profile (dry-run: no alerts, no marking seen)
python3 main.py run --profile "Lake Hartwell GA/SC" --dry-run

# CLI overrides — apply for this run only, do not modify the JSON
python3 main.py run --max-piti 3500 --min-beds 3 --assumable-only --dry-run
python3 main.py run --profile "Durham NC" --sources redfin --sources realtor

# Inspect all profiles and their affordability envelope
python3 main.py list-profiles

# Verify Twilio + SendGrid credentials
python3 main.py test-alerts

# Start the FastAPI server (future web/iOS backend)
python3 main.py serve
```

### Filter flags (all override the loaded profile for one run)

| Flag | Description |
|------|-------------|
| `--max-piti` | Max monthly PITI, e.g. `4500` |
| `--down-payment` | Down payment amount, e.g. `100000` |
| `--rate` | Interest rate as decimal, e.g. `0.065` for 6.5% |
| `--min-beds` / `--max-beds` | Bedroom range |
| `--min-baths` | Minimum bathrooms |
| `--min-sqft` / `--max-sqft` | Square footage range |
| `--min-price` / `--max-price` | Listing price range |
| `--max-hoa` | Max HOA fee/mo (`0` = no HOA only) |
| `--assumable-only` | Only listings with assumable loan keywords |
| `--has-solar` | Only listings mentioning solar |
| `--waterway-feet N` | Only listings within N feet of a stream/river |
| `--sources` | Override scrapers, e.g. `--sources redfin --sources realtor` |
| `--dry-run` | Scrape and filter but skip alerts and seen-marking |

## Configuration

### `.env` — credentials and runtime settings

```
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+1...
ALERT_TO_NUMBER=+1...
SENDGRID_API_KEY=...
ALERT_FROM_EMAIL=you@example.com
ALERT_TO_EMAIL=you@example.com
HEADLESS=true
```

### `search_profiles.json` — search criteria

Each entry is a `SearchProfile`. Key fields:

| Field | Description |
|-------|-------------|
| `name` | Display name, also used with `--profile` |
| `enabled` | Set `false` to skip on a full run |
| `zip_codes` | List of ZIP codes to search |
| `tax_region` | State, used to estimate property tax (see `app/models.py`) |
| `max_monthly_piti` | Budget ceiling including P&I, taxes, insurance, PMI |
| `down_payment` | Down payment amount |
| `interest_rate` | Market rate (overridden at runtime by live Freddie Mac PMMS) |
| `min_bedrooms` / `min_bathrooms` | Minimum requirements |
| `max_hoa_monthly` | HOA ceiling (`0` = no HOA) |
| `assumable_only` | Skip listings without assumable loan keywords |
| `requires_solar` | Skip listings without solar mention |
| `waterway_within_feet` | OSM proximity check for streams/rivers |
| `sources` | Which scrapers to use: `redfin`, `realtor`, `zillow`, `homes` |

## Alert priority

| Priority | Condition |
|----------|-----------|
| `CRITICAL` | Assumable loan detected **and** PITI ≤ budget → immediate SMS + email |
| `HIGH` | Assumable loan detected, PITI over budget → immediate SMS + email |
| `NORMAL` | No assumable loan, PITI ≤ budget → batch digest only |

## Scraper notes

| Source | Mechanism | Residential IP required? |
|--------|-----------|:---:|
| Redfin | Playwright + DOM | No |
| Realtor | HomeHarvest library | No |
| Zillow | Playwright + `__NEXT_DATA__` | Yes |
| Homes.com | Playwright + `__NEXT_DATA__` + DOM fallback | Yes |

Zillow and Homes.com emit a `WARNING` and skip cleanly when blocked — no crash.

## Deduplication

Seen listings are stored in `data/seen_listings.json`. Delete this file to reprocess all listings on the next run.

## Scheduling

See `cron.md` for system cron, Docker Compose, and macOS launchd setup.
