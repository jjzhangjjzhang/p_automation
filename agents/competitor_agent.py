"""Competitor Agent: fetches real nightly comp pricing for your own unbooked
nights and stores it for the Revenue Optimizer to consume.

Airbnb's public per-listing endpoints (bulk monthly calendar,
per-stay "book it" quote) both stopped returning prices to anonymous
requests — see the notes in scrapers/airbnb_public.py. The search results
endpoint (StaysSearch) still does, so comp pricing here works by re-running
that search once per candidate date instead of once per comp listing.

Since the Revenue Optimizer only ever needs a comp price for nights that
are actually unbooked (booked nights aren't shortlisted at all — see
revenue_optimizer.score_listing), we scope this to the union of unbooked
dates across your listings within comps.price_lookup_days, which keeps
request volume bounded regardless of how far out guardrails.pricing_window_days
reaches.
"""

import math
from datetime import date, timedelta

from scrapers import airbnb_public
from scrapers.cache import cached_get, polite_delay
from storage import db


def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r_miles = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_miles * math.asin(math.sqrt(a))


def _host_center(config: dict, client) -> tuple[float, float] | None:
    """Midpoint of our own listings' coordinates, used as the reference point
    for comp-distance filtering. Cached indefinitely — a listing's address
    doesn't move.
    """
    coords = []
    for listing in config["listings"]:
        loc = cached_get(
            f"room_location:{listing['id']}",
            ttl_hours=24 * 365,
            fetch_fn=lambda rid=listing["id"]: airbnb_public.get_room_location(client, rid),
        )
        if loc:
            coords.append(loc)
    if not coords:
        return None
    return (sum(c[0] for c in coords) / len(coords), sum(c[1] for c in coords) / len(coords))


def _unbooked_dates_within(config: dict, conn, horizon_days: int) -> set[str]:
    today = date.today()
    horizon = today + timedelta(days=horizon_days)
    dates = set()
    for listing in config["listings"]:
        rows = db.latest_own_calendar(conn, listing["id"], today.isoformat())
        for row in rows:
            d = row["date"]
            if d and not row["booked"] and date.fromisoformat(d) <= horizon:
                dates.add(d)
    return dates


def run(config: dict, conn) -> None:
    scrape_cfg = config["scrape"]
    comps_cfg = config["comps"]
    auto_cfg = comps_cfg.get("auto_discover", {})
    if not auto_cfg.get("enabled"):
        return

    price_lookup_days = comps_cfg.get("price_lookup_days", 30)
    min_nights = auto_cfg.get("min_nights", 2)
    max_distance_miles = auto_cfg.get("max_distance_miles")
    own_listing_ids = {listing["id"] for listing in config["listings"]}

    target_dates = _unbooked_dates_within(config, conn, price_lookup_days)
    if not target_dates:
        return

    client = airbnb_public.get_client()
    scraped_at = date.today().isoformat()
    host_center = _host_center(config, client) if max_distance_miles else None

    for d in sorted(target_dates):
        checkin = date.fromisoformat(d)
        checkout = checkin + timedelta(days=min_nights)
        cache_key = f"comp_price:{auto_cfg['location']}:{d}:{min_nights}"
        results = cached_get(
            cache_key,
            scrape_cfg["cache_ttl_hours"],
            lambda checkin=checkin, checkout=checkout: airbnb_public.search_listings(
                client,
                location=auto_cfg["location"],
                bedrooms_min=auto_cfg["bedrooms_min"],
                bathrooms_min=auto_cfg["bathrooms_min"],
                checkin=checkin,
                checkout=checkout,
                adults=auto_cfg.get("adults", 2),
                children=auto_cfg.get("children", 0),
                max_results=auto_cfg["max_results"],
            ),
        )

        for item in results:
            listing_id = item.get("id")
            total_price = item.get("price")
            if not listing_id or listing_id in own_listing_ids or total_price is None:
                continue
            if host_center and item.get("lat") is not None and item.get("lng") is not None:
                distance = _haversine_miles(host_center[0], host_center[1], item["lat"], item["lng"])
                if distance > max_distance_miles:
                    continue
            db.insert_comp_snapshot(
                conn,
                scraped_at=scraped_at,
                comp_listing_id=str(listing_id),
                date=d,
                price=round(total_price / min_nights, 2),
                available=1,
                min_nights=min_nights,
                source="search_by_date",
            )
        polite_delay(scrape_cfg["request_delay_seconds"])

    conn.commit()
