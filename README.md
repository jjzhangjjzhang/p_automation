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
Calendar Agent     ──┤
Availability Agent ──┘
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
- **Availability Agent** (`agents/availability_agent.py`) — decides the
  minimum-stay length and availability per unbooked night from the
  `stay_rules` policy in `config/listings.yaml`. Fully deterministic (no LLM,
  no scraping); the pipeline only *computes* the plan, see "Minimum stay and
  availability" below for pushing it.
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

## Minimum stay and availability

`agents/availability_agent.py` turns the per-listing `stay_rules` policy into a
per-night plan: `{date: {available, min_nights, reasons, flags}}`. Every rule in
the config is a **relaxation** of `default_min_nights` — a date's effective
minimum is the smallest value any matching rule allows.

The two listings share everything except how they treat a single orphan night.
The shared part is the `stay_rules_presets.base` YAML anchor, merged into each
listing with `<<`, and the `one_night_gap` rule is chosen per listing.

| Rule | Cascade | Dennison |
| --- | --- | --- |
| `default_min_nights` | 3 | 3 |
| `near_term: {days: 14, min_nights: 2}` | 2 nights for check-ins in the next two weeks | same |
| `weekday_min_nights: {friday: 2}` | 2 nights for a Friday check-in, any time | same |
| `two_night_gap_min_nights: 2` | 2 nights when exactly 2 unbooked nights sit between bookings | same |
| `one_night_gap` | `turnover_toggle`: 1-night stay; on a Mon–Fri run bookable **only 17:00–07:00**, on a Sat/Sun run **open all day** | `open`: 1-night stay, always bookable |

Note that `stay_rules.*.min_nights` (your own listings' minimum stay, pushed to
your calendar) and `comps.auto_discover.min_nights` (the stay length competitor
searches are run with) are unrelated settings that happen to share a name.

A "gap" means a run of unbooked nights with a booking on **both** sides; a run at
the start or end of the scraped calendar is never treated as a gap, since
there's no evidence it's a hole rather than the edge of what we scraped.

Dates where the minimum can't be met (e.g. checking in on the last night of a
3-night gap leaves only 1 night) are **flagged, not silently relaxed** — whether
to take that stay is a policy call, so the agent reports it instead of deciding.

### The single-night turnover window (Cascade)

Airbnb has no hourly availability and no per-date check-in/check-out times, so
`action: turnover_toggle` is implemented by *re-running the apply script on a
schedule*: at `open_at` it opens the orphan night, at `close_at` it blocks it
again. **Both runs are required** — one daily run leaves the night stuck in
whichever state it was last left in.

`open_all_day_on: [saturday, sunday]` suspends the window on those weekdays:
**every** orphan night stays bookable around the clock, and the 7am run leaves
them open instead of blocking them.

That weekday is matched against **the day the agent runs**, not against the
orphan night's own date — so a Saturday run opens every orphan night, whichever
weekday each one happens to fall on. In practice the calendar opens at Friday
17:00 and stays open through the weekend, then closes at Monday 07:00:

| Run day | 00h | 06h | 07h | 12h | 16h | 17h | 23h |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Mon–Fri | open | open | **block** | **block** | **block** | open | open |
| Sat, Sun | open | open | open | open | open | open | open |

Dennison uses `action: open` instead, so its orphan nights need no schedule —
they're simply bookable at `min_nights: 1`. It still needs a periodic run to
*set* that minimum on newly-formed gaps, which the cron below covers for both
listings.

```bash
python scripts/apply_availability.py --plan-only          # no browser, just the plan
python scripts/apply_availability.py                      # dry run against Airbnb
python scripts/apply_availability.py --apply              # actually save
python scripts/apply_availability.py --listing Cascade --apply --no-headless
```

Cron both edges of Cascade's 17:00–07:00 window:

```cron
0 7  * * * cd /path/to/p_automation && .venv/bin/python scripts/apply_availability.py --apply
0 17 * * * cd /path/to/p_automation && .venv/bin/python scripts/apply_availability.py --apply
```

Every real change is logged to the `availability_actions` table. That log is
load-bearing, not just an audit trail: the host calendar scrape reports one
`booked` flag and **cannot** distinguish a guest reservation from a night the
agent blocked itself, so the apply script reads its own open blocks back and
un-marks them before planning. Without it, an agent-blocked night would both
get stranded and be mistaken for a booking boundary by the gap detector.

### Caveats

- **The day-panel selectors are unverified.** `set_price` was derived from a
  live session, but `set_availability` / `set_min_nights` were not — their
  selectors (`_AVAILABILITY_TOGGLES`, `_MIN_NIGHTS_INPUTS` in
  `scrapers/airbnb_host.py`) are candidate lists. Run with `--no-headless`
  first; if every date fails with a selector error, re-derive them with
  `scripts/dump_host_pages.py`.
- A listing with no `stay_rules` block is **skipped** by the agent rather than
  given guessed defaults.
- The scraper can't read per-day minimum-stay values, so the first `--apply`
  run opens each night's panel once to learn them; later runs skip dates
  already at target. Use `--days` / `--limit` to bound a run.

## Tests

```bash
python3 -m unittest discover -s tests -v   # stdlib only, no venv needed
```

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
- `apply_availability.py` — pushes the Availability Agent's minimum-stay and
  availability plan to Airbnb (see above). Dry run by default.
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
