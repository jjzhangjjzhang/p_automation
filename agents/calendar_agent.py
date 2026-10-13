"""Calendar Agent: finds orphan gap nights in your own calendar — short runs
of unbooked nights sandwiched between bookings — since those are the
hardest inventory to sell and benefit most from a price nudge.
"""

from datetime import date, timedelta

MAX_GAP_LENGTH = 2  # a run of unbooked nights this short or shorter, bordered by bookings, counts as a "gap"


def find_gap_nights(own_calendar_rows: list[dict]) -> set[str]:
    """own_calendar_rows: rows shaped like {date, booked}, sorted or unsorted.

    Returns the set of date strings (YYYY-MM-DD) flagged as gap nights: a run
    of at most MAX_GAP_LENGTH unbooked nights with a booked night on both
    sides. Runs at the very start/end of the calendar (no booking on one
    side) are never flagged, since there's no evidence they're actually a
    "hole" rather than just the edge of known availability.
    """
    by_date = {row["date"]: bool(row["booked"]) for row in own_calendar_rows if row.get("date")}
    if not by_date:
        return set()

    all_dates = sorted(date.fromisoformat(d) for d in by_date)
    start, end = all_dates[0], all_dates[-1]

    gaps = set()
    run: list[date] = []
    prev_booked = False  # no booking known before `start`, so a leading run can't be a gap

    def flush_run(next_booked: bool):
        if run and len(run) <= MAX_GAP_LENGTH and prev_booked and next_booked:
            gaps.update(d.isoformat() for d in run)

    d = start
    while d <= end:
        is_booked = by_date.get(d.isoformat(), False)
        if is_booked:
            flush_run(next_booked=True)
            run = []
            prev_booked = True
        else:
            run.append(d)
        d += timedelta(days=1)
    flush_run(next_booked=False)  # trailing run: no booking known after `end`, so never a gap

    return gaps


def find_at_risk_nights(own_calendar_rows: list[dict], lookout_days: int = 7) -> dict[str, float]:
    """Nights that aren't gap nights (yet), but sit close enough to an
    existing booking that they're at risk of becoming one — e.g. the night
    right after a checkout, before anything else has booked around it.

    Returns {date: risk_weight}, where weight is 1.0 immediately adjacent to
    a booking and tapers linearly to 0 at lookout_days away. The idea is to
    price these nights a little more attractively *before* a gap forms,
    rather than waiting for find_gap_nights to flag an already-orphaned
    night. Dates already flagged as gap nights are excluded here — those
    get the full gap-night boost instead of a tapered one.
    """
    by_date = {row["date"]: bool(row["booked"]) for row in own_calendar_rows if row.get("date")}
    if not by_date:
        return {}

    gap_nights = find_gap_nights(own_calendar_rows)
    all_dates = sorted(date.fromisoformat(d) for d in by_date)
    start, end = all_dates[0], all_dates[-1]

    at_risk: dict[str, float] = {}
    d = start
    while d <= end:
        if by_date.get(d.isoformat(), False):
            for direction in (1, -1):
                for distance in range(1, lookout_days + 1):
                    neighbor = d + timedelta(days=direction * distance)
                    neighbor_str = neighbor.isoformat()
                    if neighbor_str not in by_date or by_date[neighbor_str]:
                        break  # hit a booked night or the edge of known calendar
                    if neighbor_str not in gap_nights:
                        weight = 1 - (distance - 1) / lookout_days
                        at_risk[neighbor_str] = max(at_risk.get(neighbor_str, 0.0), weight)
        d += timedelta(days=1)

    return at_risk
