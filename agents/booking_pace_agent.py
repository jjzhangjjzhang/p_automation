"""Booking Pace Agent: scrapes your own listings' calendar/reservations, then
compares current occupancy-on-the-books against a historical baseline for
the same lead time.

The pace model here is a deliberately simple first pass: it compares "% of
the pricing window currently booked" against an empirical baseline built
from how far in advance past bookings were typically made. Treat the
pace_ratio as directional (behind/on-pace/ahead), not a precise forecast —
refine once a season of real data has accumulated.
"""

from datetime import date, datetime, timedelta

from scrapers.airbnb_host import HostSession
from storage import db


def _historical_lead_time_cdf(bookings: list[dict]) -> list[int]:
    """Lead time (days between booking made and checkin) for each past booking."""
    lead_times = []
    for b in bookings:
        if not b["booked_at"]:
            continue
        checkin = date.fromisoformat(b["checkin"])
        booked_at = date.fromisoformat(b["booked_at"])
        lead_times.append((checkin - booked_at).days)
    return sorted(lt for lt in lead_times if lt >= 0)


def _expected_booked_fraction(lead_time_cdf: list[int], lead_time_days: int) -> float:
    """Empirical P(a night this far out was already booked historically)."""
    if not lead_time_cdf:
        return 0.5  # no history yet — neutral prior
    booked_by_now = sum(1 for lt in lead_time_cdf if lt >= lead_time_days)
    return booked_by_now / len(lead_time_cdf)


def run(config: dict, conn) -> dict:
    """Scrapes pace data for all listings, stores it, and returns
    {listing_id: pace_ratio} where >1 means ahead of historical pace,
    <1 means behind.
    """
    window_days = config["guardrails"]["pricing_window_days"]
    lookback_days = config["pace"]["lookback_days_for_history"]
    scraped_at = datetime.now().isoformat()
    today = date.today()

    pace_by_listing = {}

    with HostSession(headless=True) as session:
        session.login()

        for listing in config["listings"]:
            listing_id = listing["id"]

            calendar_days = session.get_calendar(listing_id, months=window_days // 30 + 1)
            for day in calendar_days:
                db.insert_own_calendar(
                    conn,
                    scraped_at=scraped_at,
                    listing_id=listing_id,
                    date=day["date"],
                    price=day["price"],
                    booked=1 if day["booked"] else 0,
                    min_nights=day["min_nights"],
                )

            reservations = session.get_reservations(listing_id)
            for r in reservations:
                conn.execute(
                    """INSERT INTO bookings (listing_id, checkin, checkout, booked_at, nightly_rate, total_price)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (listing_id, r["checkin"], r["checkout"], r["booked_at"], r["nightly_rate"], r["total_price"]),
                )

            past_bookings = conn.execute(
                "SELECT * FROM bookings WHERE listing_id = ?", (listing_id,)
            ).fetchall()
            lead_time_cdf = _historical_lead_time_cdf(past_bookings)

            window_end = today + timedelta(days=window_days)
            window_calendar = [
                d for d in calendar_days if d.get("date") and date.fromisoformat(d["date"]) <= window_end
            ]
            if window_calendar:
                actual_booked_fraction = sum(1 for d in window_calendar if d["booked"]) / len(window_calendar)
                avg_lead_time = window_days // 2
                expected_booked_fraction = _expected_booked_fraction(lead_time_cdf, avg_lead_time)
                pace_ratio = (actual_booked_fraction + 1e-6) / (expected_booked_fraction + 1e-6)
            else:
                pace_ratio = 1.0

            pace_by_listing[listing_id] = pace_ratio

    conn.commit()
    return pace_by_listing
