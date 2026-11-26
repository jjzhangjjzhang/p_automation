"""Availability Agent: decides, per listing per unbooked night, what the
minimum-stay setting and availability should be.

Unlike the pricing agents this one is *purely* deterministic — no LLM, no
scraping. It takes the own-calendar rows that the Booking Pace Agent already
wrote to the `own_calendar` table and turns them into a per-date stay plan:

    {date: {available, min_nights, reasons, flags, turnover_window}}

Rules live per listing under `stay_rules` in config/listings.yaml so the two
listings can diverge. Every rule in the config is a *relaxation* of
`default_min_nights`: the effective minimum for a date is the smallest
minimum any matching rule allows. A listing with no `stay_rules` block is
skipped entirely rather than being given a guessed default.

Gap semantics match agents/calendar_agent.py: a "gap" is a run of unbooked
nights with a booked night on *both* sides. A run at the start or end of the
known calendar is not a gap — there's no evidence it's a hole rather than
just the edge of what we scraped — so gap relaxations never apply there.

Caveat on `available`: the host calendar scrape reports a single `booked`
flag and can't distinguish a guest booking from a host-blocked night, so
this agent only ever reasons about nights it sees as unbooked. That means a
night this agent blocked on a previous run comes back as `booked` and
drops out of the plan -- see the `stuck_blocked` note in
scripts/apply_availability.py.

The `turnover_toggle` action makes a date's availability a function of the
*current clock time and weekday*, so the plan is only valid for the moment it
was computed. Its `open_all_day_on` weekdays are matched against when the agent
RUNS, not against the night being planned: on those days every orphan night is
open around the clock. Re-run on a schedule (both at open_at and at close_at) to move the
calendar; a single daily run will leave the night stuck in one state.
"""

from datetime import date, datetime, time, timedelta

WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _parse_weekday(key) -> int:
    """Accepts 'friday', 'Friday', 'fri' or 4."""
    if isinstance(key, int):
        if 0 <= key <= 6:
            return key
        raise ValueError(f"weekday_min_nights: {key} is not 0-6 (Monday-Sunday)")
    name = str(key).strip().lower()
    if name in WEEKDAY_NAMES:
        return WEEKDAY_NAMES[name]
    for full, idx in WEEKDAY_NAMES.items():
        if full.startswith(name) and len(name) >= 3:
            return idx
    raise ValueError(f"weekday_min_nights: {key!r} is not a weekday name or 0-6")


def _parse_clock(value) -> time:
    """'17:00' / '17:00:00' / '5' -> datetime.time."""
    text = str(value).strip()
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except ValueError as e:
        raise ValueError(f"Not a HH:MM clock time: {value!r}") from e
    return time(hour, minute)


def _in_window(now: time, open_at: time, close_at: time) -> bool:
    """Is `now` inside [open_at, close_at)? The window may wrap midnight, which
    is the normal case here (17:00 -> 07:00)."""
    if open_at == close_at:
        return True  # degenerate 24h window
    if open_at < close_at:
        return open_at <= now < close_at
    return now >= open_at or now < close_at  # wraps midnight


def _bounded_runs(by_date: dict[str, bool]) -> list[list[date]]:
    """Runs of consecutive unbooked nights that have a booked night on both
    sides. Dates missing from the calendar end a run without bounding it, so
    a hole in the scrape can't be mistaken for a booking.
    """
    if not by_date:
        return []
    all_dates = sorted(date.fromisoformat(d) for d in by_date)
    start, end = all_dates[0], all_dates[-1]

    runs: list[list[date]] = []
    run: list[date] = []
    prev_booked = False  # nothing known before `start`, so a leading run isn't bounded

    d = start
    while d <= end:
        key = d.isoformat()
        if key not in by_date:
            run, prev_booked = [], False  # gap in the scrape: not a boundary
        elif by_date[key]:
            if run and prev_booked:
                runs.append(run)
            run, prev_booked = [], True
        else:
            run.append(d)
        d += timedelta(days=1)
    # trailing run deliberately dropped: nothing known after `end` to bound it
    return runs


def _nights_until_next_booking(d: date, by_date: dict[str, bool]) -> int | None:
    """How many consecutive unbooked nights start at `d`, or None if the
    calendar runs out before a booked night is found.
    """
    n = 0
    cur = d
    while True:
        key = cur.isoformat()
        if key not in by_date:
            return None
        if by_date[key]:
            return n
        n += 1
        cur += timedelta(days=1)


def plan_listing(own_calendar_rows: list[dict], rules: dict, now: datetime | None = None) -> dict[str, dict]:
    """Returns {date: plan} for every unbooked night in the calendar.

    plan = {
        available: bool,
        min_nights: int,
        reasons: [str],          # which relaxations fired, for the audit trail
        flags: [str],            # things needing a human (e.g. same-day turnover)
        turnover_window: str|None,
    }
    """
    now = now or datetime.now()
    today = now.date()
    by_date = {r["date"]: bool(r["booked"]) for r in own_calendar_rows if r.get("date")}
    if not by_date:
        return {}

    default_min = int(rules["default_min_nights"])
    near_term = rules.get("near_term") or {}
    weekday_rules = {_parse_weekday(k): int(v) for k, v in (rules.get("weekday_min_nights") or {}).items()}
    gap_rules = rules.get("gap_rules") or {}
    two_night_gap_min = gap_rules.get("two_night_gap_min_nights")
    one_night_gap = gap_rules.get("one_night_gap") or {}
    open_all_day_on = {_parse_weekday(k) for k in (one_night_gap.get("open_all_day_on") or [])}

    gap_len_by_date: dict[str, int] = {}
    for run in _bounded_runs(by_date):
        for d in run:
            gap_len_by_date[d.isoformat()] = len(run)

    plans: dict[str, dict] = {}
    for date_str, booked in sorted(by_date.items()):
        if booked:
            continue
        d = date.fromisoformat(date_str)

        min_nights = default_min
        reasons: list[str] = []
        flags: list[str] = []
        available = True
        turnover_window = None

        def relax(candidate, reason):
            nonlocal min_nights
            if candidate is not None and int(candidate) < min_nights:
                min_nights = int(candidate)
                reasons.append(reason)

        if near_term.get("days") is not None and near_term.get("min_nights") is not None:
            horizon = today + timedelta(days=int(near_term["days"]))
            if today <= d <= horizon:
                relax(near_term["min_nights"], f"within next {int(near_term['days'])} days")

        if d.weekday() in weekday_rules:
            relax(weekday_rules[d.weekday()], f"{d.strftime('%A')} check-in")

        gap_len = gap_len_by_date.get(date_str)
        if gap_len == 2:
            relax(two_night_gap_min, "2-night gap between bookings")
        elif gap_len == 1 and one_night_gap:
            action = str(one_night_gap.get("action", "open")).lower()
            if action == "block":
                available = False
                reasons.append("1-night gap between bookings (blocked by rule)")
            else:
                relax(one_night_gap.get("min_nights", 1), "1-night gap between bookings")
                if action == "turnover_toggle":
                    # NOTE: open_all_day_on is matched against the weekday the
                    # agent is RUNNING on (now), not against the orphan night's
                    # own date. On those days the clock window is suspended and
                    # every orphan night stays bookable around the clock.
                    if now.weekday() in open_all_day_on:
                        available = True
                        reasons.append(
                            f"1-night gap, window suspended on {now.strftime('%A')} (open all day)"
                        )
                    else:
                        open_at = _parse_clock(one_night_gap.get("open_at", "17:00"))
                        close_at = _parse_clock(one_night_gap.get("close_at", "07:00"))
                        available = _in_window(now.time(), open_at, close_at)
                        turnover_window = f"{open_at.strftime('%H:%M')}-{close_at.strftime('%H:%M')}"
                        state = "inside" if available else "outside"
                        reasons.append(f"1-night gap, turnover window {turnover_window} ({state} it now)")
                if one_night_gap.get("note"):
                    flags.append(one_night_gap["note"])

        # A check-in that can't satisfy its own minimum is dead inventory. Flag
        # it rather than silently relaxing the minimum -- that's a pricing/
        # policy call for the host, not something to infer.
        runway = _nights_until_next_booking(d, by_date)
        if available and runway is not None and runway < min_nights:
            flags.append(f"unbookable as check-in: only {runway} night(s) before the next booking, min is {min_nights}")

        plans[date_str] = {
            "available": available,
            "min_nights": min_nights,
            "reasons": reasons,
            "flags": flags,
            "turnover_window": turnover_window,
        }
    return plans


def run(config: dict, conn, now: datetime | None = None) -> dict[str, dict]:
    """Pipeline entry point: {listing_id: {date: plan}}.

    Reads the latest own-calendar snapshot per listing. Listings with no
    `stay_rules` config are skipped (and reported) rather than defaulted.
    """
    from storage import db

    now = now or datetime.now()
    today = now.date()
    out: dict[str, dict] = {}
    for listing in config["listings"]:
        rules = listing.get("stay_rules")
        if not rules:
            print(f"  {listing['name']}: no stay_rules configured, skipping")
            continue
        rows = [dict(r) for r in db.latest_own_calendar(conn, listing["id"], today.isoformat())]
        plans = plan_listing(rows, rules, now)
        out[listing["id"]] = plans
        changed = sum(1 for p in plans.values() if p["reasons"] or p["flags"])
        print(f"  {listing['name']}: {len(plans)} unbooked nights, {changed} with a non-default rule or flag")
    return out
