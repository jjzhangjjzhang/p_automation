# p_automation

Daily dynamic-pricing pipeline for two Airbnb listings ("Cascade" and
"Dennison", San Diego). Scrapes your own booking calendar and live
competitor pricing, scores candidate prices per unbooked night, and asks
Claude to pick a final recommended price per listing per date with a
plain-language rationale. Everything upstream of the final decision step
is deterministic; prices are never pushed to Airbnb automatically.

## Quick reference: get the two pricing worksheet CSVs

```bash
source .venv/bin/activate
python pipeline.py
python scripts/build_pricing_worksheets.py
```

Writes `outputs/pricing_worksheet_<date>_cascade.csv` and
`outputs/pricing_worksheet_<date>_dennison.csv`.

## Pipeline

```
Booking Pace Agent ──┐
Competitor Agent   ───┼──> Revenue Optimizer ──> LLM Decision Agent
Calendar Agent     ──┘
```

- **Booking Pace Agent** (`agents/booking_pace_agent.py`) — scrapes your own
  listings' calendar/reservations and compares current on-the-books
  occupancy against a historical baseline for the same lead time.
- **Competitor Agent** (`agents/competitor_agent.py`) — fetches live comp
  pricing (via Airbnb's search endpoint) for your own unbooked nights.
- **Calendar Agent** (`agents/calendar_agent.py`) — flags gap nights and
  at-risk nights from your own calendar.
- **Revenue Optimizer** (`agents/revenue_optimizer.py`) — scores a
  shortlist of candidate prices per night from the signals above, subject
  to guardrails, and coordinates pricing between the two twin listings.
- **LLM Decision Agent** (`agents/decision_agent.py`) — the only step that
  calls an LLM (Claude): picks one final price per listing per date from
  the shortlist and explains the rationale.

Run the whole thing with:

```bash
python pipeline.py
```

This writes recommendations to the `recommendations` table in
`data/pricing.db` and to `outputs/recommendations/<date>.json`.

## Getting prices onto Airbnb

Nothing in `pipeline.py` touches Airbnb's pricing calendar. The apply step
is manual and separate:

1. `python scripts/build_pricing_worksheets.py` — builds one CSV per
   listing in `outputs/` with comp pricing, your current price, and the
   latest recommendation side by side, plus a blank `new_price` column.
2. Fill in `new_price` for the nights you want to change.
3. `python scripts/apply_prices_from_csv.py` — dry run by default (opens
   each night's edit panel, reports what it would change, clicks Cancel).
   Pass `--apply` to actually save changes to Airbnb.

## One-time setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env   # fill in ANTHROPIC_API_KEY
```

Log into Airbnb once so the Booking Pace / apply-prices scripts can reuse
the session:

```bash
python scripts/airbnb_host_login.py
```

Opens a real browser for you to log in manually (handles 2FA), then saves
the session to `data/.airbnb_auth_state.json`. Re-run whenever
`HostSession` reports the saved session has expired.

## Config

`config/listings.yaml` — listing IDs/names, comp-discovery search
parameters, pricing guardrails (floor/ceiling, max daily change %, min
price gap between the two listings), and scrape politeness settings.

## Storage

SQLite at `data/pricing.db` (schema in `storage/schema.sql`): own-calendar
snapshots, comp-price snapshots, and recommendations history.

## Maintenance scripts (`scripts/`)

- `airbnb_host_login.py` — one-time/periodic manual login (see above).
- `build_pricing_worksheets.py` / `apply_prices_from_csv.py` — the manual
  apply step (see above).
- `capture_hashes.py` — attaches to a Chrome instance running with
  `--remote-debugging-port=9222` and prints the current Airbnb GraphQL
  persisted-query hashes, needed when `scrapers/airbnb_public.py` starts
  404ing because Airbnb shipped a new frontend build.
- `dump_host_pages.py`, `test_host_scraper.py` — ad-hoc debugging tools for
  reverse-engineering/verifying the host-dashboard scraper; not part of
  the pipeline.

## Notes

- `scrapers/airbnb_public.py` hits Airbnb's internal (unofficial) search
  API and needs its GraphQL query hash refreshed occasionally — see
  `capture_hashes.py` above.
- `scrapers/airbnb_host.py` logs in as you, so it's normal authenticated
  browsing rather than scraping someone else's data.
- The booking-pace model is a deliberately simple first pass (directional,
  not a precise forecast) — see the docstring in `booking_pace_agent.py`.
- This is for personal/internal use managing your own listings; automated
  interaction with Airbnb may be subject to their Terms of Service.
