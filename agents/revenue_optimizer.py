"""Revenue Optimizer: deterministic candidate-price scoring.

For each night and each listing, builds a grid of candidate prices, scores
each by estimated booking probability x price (expected revenue), and keeps
a shortlist. Then applies the twin-listing coordination rule across the two
listings for the same date, since they compete for the same guest search.

This is a simple, transparent heuristic model meant to be tuned once real
outcomes (bookings vs. price) start accumulating — not a trained model.
"""

import math
import statistics
from datetime import date, timedelta


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _comp_median_by_date(comp_snapshots: list[dict]) -> dict[str, float]:
    by_date: dict[str, list[float]] = {}
    for row in comp_snapshots:
        if row.get("price") is None or not row.get("available"):
            continue
        by_date.setdefault(row["date"], []).append(row["price"])
    return {d: statistics.median(prices) for d, prices in by_date.items() if prices}


def _comp_min_by_date(comp_snapshots: list[dict]) -> dict[str, float]:
    by_date: dict[str, list[float]] = {}
    for row in comp_snapshots:
        if row.get("price") is None or not row.get("available"):
            continue
        by_date.setdefault(row["date"], []).append(row["price"])
    return {d: min(prices) for d, prices in by_date.items() if prices}


def _candidate_grid(current_price: float, guardrails: dict) -> list[float]:
    max_change = guardrails["max_daily_change_pct"] / 100
    steps = [-max_change, -max_change * 0.66, -max_change * 0.33, 0, max_change * 0.33, max_change * 0.66, max_change]
    candidates = [current_price * (1 + step) for step in steps]
    return [
        max(guardrails["price_floor"], min(guardrails["price_ceiling"], round(c)))
        for c in candidates
    ]


def _booking_probability(
    candidate_price: float, comp_baseline: float, pace_ratio: float, is_gap_night: bool, is_recent: bool, at_risk_weight: float = 0.0
) -> float:
    """Heuristic, not a fitted model. For recent dates, comparing against the minimum comp rate with a steeper curve maximizes probability. For dates further out, comparing against the median is used.
    """
    price_delta_pct = (candidate_price - comp_baseline) / comp_baseline if comp_baseline else 0
    urgency = 1.0
    if pace_ratio < 0.9:
        urgency += 0.5
    if is_gap_night:
        urgency += 0.5
    else:
        urgency += 0.5 * at_risk_weight
    
    curve_multiplier = 20 if is_recent else 3
    return _sigmoid(-curve_multiplier * urgency * price_delta_pct)


def score_listing(
    listing_id: str,
    own_calendar_rows: list[dict],
    comp_snapshots: list[dict],
    pace_ratio: float,
    gap_nights: set[str],
    guardrails: dict,
    at_risk_nights: dict[str, float] | None = None,
) -> dict[str, list[dict]]:
    """Returns {date: [ {price, expected_prob, expected_revenue}, ... top 3 ] }."""
    comp_min = _comp_min_by_date(comp_snapshots)
    comp_median = _comp_median_by_date(comp_snapshots)
    window_end = date.today() + timedelta(days=guardrails["pricing_window_days"])
    at_risk_nights = at_risk_nights or {}

    shortlists = {}
    for row in own_calendar_rows:
        d = row.get("date")
        if not d or row.get("booked") or date.fromisoformat(d) > window_end:
            continue
        current_price = row.get("price") or guardrails["price_floor"]
        is_recent = (date.fromisoformat(d) - date.today()).days <= 14
        
        comp_baseline = comp_min.get(d, current_price) if is_recent else comp_median.get(d, current_price)
        is_gap = d in gap_nights
        risk_weight = at_risk_nights.get(d, 0.0)

        scored = []
        for candidate in _candidate_grid(current_price, guardrails):
            prob = _booking_probability(candidate, comp_baseline, pace_ratio, is_gap, is_recent, risk_weight)
            scored.append(
                {
                    "price": candidate,
                    "expected_prob": round(prob, 3),
                    "expected_revenue": round(candidate * prob, 2),
                }
            )
        
        if is_recent:
            scored.sort(key=lambda s: s["expected_prob"], reverse=True)
        else:
            scored.sort(key=lambda s: s["expected_revenue"], reverse=True)
        shortlists[d] = scored[:3]

    return shortlists


def coordinate_twin_listings(
    shortlists_by_listing: dict[str, dict[str, list[dict]]],
    pace_by_listing: dict[str, float],
    min_gap: float,
    guardrails: dict | None = None,
) -> dict[str, dict[str, list[dict]]]:
    """Enforces a minimum price gap between two listings on the same date, so
    they never tie and effectively split the same guest search 50/50.
    Whichever listing is further behind pace gets steered to be the cheaper
    one for that date, since it needs the booking more.
    """
    listing_ids = list(shortlists_by_listing.keys())
    if len(listing_ids) != 2:
        return shortlists_by_listing  # coordination only defined for the 2-listing case

    guardrails = guardrails or {}
    price_ceiling = guardrails.get("price_ceiling", math.inf)
    price_floor = guardrails.get("price_floor", -math.inf)

    a_id, b_id = listing_ids
    a_shortlists, b_shortlists = shortlists_by_listing[a_id], shortlists_by_listing[b_id]
    a_needier = pace_by_listing.get(a_id, 1.0) <= pace_by_listing.get(b_id, 1.0)

    shared_dates = set(a_shortlists) & set(b_shortlists)
    for d in shared_dates:
        a_best, b_best = a_shortlists[d][0], b_shortlists[d][0]
        if abs(a_best["price"] - b_best["price"]) >= min_gap:
            continue

        cheaper, pricier = (a_best, b_best) if a_needier else (b_best, a_best)
        # Pushing pricier up by min_gap can break the ceiling; if so, pin
        # pricier to the ceiling and pull cheaper down instead, so the gap
        # is preserved without violating either guardrail.
        if cheaper["price"] + min_gap > price_ceiling:
            pricier["price"] = price_ceiling
            cheaper["price"] = max(price_floor, price_ceiling - min_gap)
        else:
            pricier["price"] = cheaper["price"] + min_gap

    return {a_id: a_shortlists, b_id: b_shortlists}
