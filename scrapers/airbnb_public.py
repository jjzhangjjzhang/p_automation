"""Scrapes Airbnb's public, unauthenticated search endpoint for comp pricing.

This hits the same internal JSON endpoint the website itself calls (not an
official API). Two things will need to be refreshed periodically by you,
since Airbnb rotates them without notice:

1. The API key (`_get_api_key`) — scraped live from the homepage, so this
   should keep working on its own.
2. `STAYS_SEARCH_HASH` — a per-query GraphQL operation hash that occasionally
   changes when Airbnb ships a new frontend build. If a request starts
   returning 400/404, open a real search page in a browser, open devtools >
   Network, filter for "StaysSearch", and copy the new `sha256Hash` — as of
   this writing it's embedded in the URL *path* (`/api/v3/StaysSearch/<hash>`)
   and the request is a POST with the GraphQL variables in the JSON body.

NOTE: Airbnb's other public per-listing endpoints (bulk monthly calendar via
PdpAvailabilityCalendar, per-stay quote via StaysPdpBookItQuery) have both
stopped returning prices to anonymous requests — confirmed by direct testing,
not just missing fields. StaysSearch is the only one still exposing real
prices, which is why comp pricing here works by searching per-date rather
than fetching each comp listing's own calendar.
"""

import base64
import json
import re
import ssl
from datetime import date

import httpx
import truststore

BASE_URL = "https://www.airbnb.com"
STAYS_SEARCH_HASH = "a607fc0d39cdab40dece08de7ba484a17ec5ebb81b355bc5c9816f8d7fb0cf23"
# Anonymous per-listing availability calendar (no prices, just open/blocked
# days + min nights). Rotates like STAYS_SEARCH_HASH — refresh via
# scripts/capture_hashes.py (look for PdpAvailabilityCalendarV2Query).
AVAILABILITY_CALENDAR_HASH = "4d4724885b57d91f65be3c50c8b792b92a0dc723a871affb6ee358a0caf6d694"

# Fields the persisted StaysSearch query validates as required, mirrored from a
# real browser request — omitting any of these (or the staysMapSearchRequestV2
# companion block) causes a GraphQL ValidationError, not a clean 400.
_SEARCH_TREATMENT_FLAGS = [
    "feed_map_decouple_m11_treatment",
    "recommended_amenities_2024_treatment_b",
    "filter_redesign_2024_treatment",
    "filter_reordering_2024_roomtype_treatment",
    "p2_category_bar_removal_treatment",
    "selected_filters_2024_treatment",
    "recommended_filters_2024_treatment_b",
    "m13_search_input_phase2_treatment",
    "m13_search_input_services_enabled",
    "m13_2025_experiences_p2_treatment",
    "homes_p25_refresh_2025_treatment",
]

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def get_client() -> httpx.Client:
    # Use the OS trust store so corporate TLS-inspecting proxies (self-signed
    # chain) don't cause CERTIFICATE_VERIFY_FAILED.
    ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.Client(headers=_DEFAULT_HEADERS, timeout=20.0, follow_redirects=True, verify=ssl_ctx)


def _get_api_key(client: httpx.Client) -> str:
    resp = client.get(BASE_URL)
    resp.raise_for_status()
    match = re.search(r'"api_config":\s*{\s*"key":\s*"([^"]+)"', resp.text)
    if not match:
        raise RuntimeError(
            "Could not find Airbnb's embedded api_config.key in the homepage HTML. "
            "Airbnb likely changed their page structure — inspect the page source "
            "for the new pattern and update _get_api_key()."
        )
    return match.group(1)


def search_listings(
    client: httpx.Client,
    location: str,
    bedrooms_min: int,
    bathrooms_min: int,
    checkin: date,
    checkout: date,
    adults: int = 2,
    children: int = 0,
    max_results: int = 15,
) -> list[dict]:
    """Runs an Airbnb search and returns comp candidates.

    Returns a list of dicts: {id, name, price, rating, review_count, lat, lng}.
    """
    api_key = _get_api_key(client)

    def _raw_params(with_grid: bool) -> list[dict]:
        params = [
            {"filterName": "query", "filterValues": [location]},
            {"filterName": "checkin", "filterValues": [checkin.isoformat()]},
            {"filterName": "checkout", "filterValues": [checkout.isoformat()]},
            {"filterName": "adults", "filterValues": [str(adults)]},
            {"filterName": "minBedrooms", "filterValues": [str(bedrooms_min)]},
            {"filterName": "minBathrooms", "filterValues": [str(bathrooms_min)]},
            {"filterName": "roomTypes", "filterValues": ["Entire home/apt"]},
            {"filterName": "refinementPaths", "filterValues": ["/homes"]},
        ]
        if children:
            params.append({"filterName": "children", "filterValues": [str(children)]})
        if with_grid:
            params.append({"filterName": "itemsPerGrid", "filterValues": [str(max_results)]})
        return params

    def _search_request_block(with_grid: bool) -> dict:
        return {
            "metadataOnly": False,
            "requestedPageType": "STAYS_SEARCH",
            "searchType": "unknown",
            "source": "structured_search_input_header",
            "treatmentFlags": _SEARCH_TREATMENT_FLAGS,
            **({"maxMapItems": 9999} if not with_grid else {}),
            "rawParams": _raw_params(with_grid),
        }

    body = {
        "operationName": "StaysSearch",
        "variables": {
            "staysSearchRequest": _search_request_block(with_grid=True),
            "staysMapSearchRequestV2": _search_request_block(with_grid=False),
            "isLeanTreatment": False,
            "aiSearchEnabled": False,
        },
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": STAYS_SEARCH_HASH}},
    }
    resp = client.post(
        f"{BASE_URL}/api/v3/StaysSearch/{STAYS_SEARCH_HASH}",
        params={"operationName": "StaysSearch", "locale": "en", "currency": "USD"},
        json=body,
        headers={
            "X-Airbnb-Api-Key": api_key,
            "Content-Type": "application/json",
            "X-Airbnb-Graphql-Platform": "web",
            "X-Airbnb-Graphql-Platform-Client": "minimalist-niobe",
            "X-Csrf-Without-Token": "1",
        },
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    sections = data.get("data", {}).get("presentation", {}).get("staysSearch", {}).get("results", {}).get(
        "searchResults", []
    )
    for item in sections[:max_results]:
        demand_listing = item.get("demandStayListing") or {}
        results.append(
            {
                "id": _decode_listing_id(demand_listing.get("id")),
                "name": item.get("title"),
                "price": _parse_display_price(item.get("structuredDisplayPrice")),
                "rating": item.get("avgRatingLocalized"),
                "review_count": None,
                "lat": (demand_listing.get("location") or {}).get("coordinate", {}).get("latitude"),
                "lng": (demand_listing.get("location") or {}).get("coordinate", {}).get("longitude"),
            }
        )
    return results


def get_availability(client: httpx.Client, room_id: str, months: int = 5) -> list[dict]:
    """Fetches a listing's public availability calendar, no login needed.

    Returns [{date, booked, min_nights}] starting from the current month.
    `booked` is True for any night guests can't book (reserved OR host-blocked
    — the public calendar can't tell the two apart). Prices aren't exposed
    here, so price is None.
    """
    api_key = _get_api_key(client)
    global_id = base64.b64encode(f"DemandStayListing:{room_id}".encode()).decode()
    today = date.today()
    variables = {
        "listingId": global_id,
        "input": {
            "count": months + 1,
            "month": today.month,
            "year": today.year,
            "returnPropertyLevelCalendarIfApplicable": False,
        },
    }
    extensions = {"persistedQuery": {"version": 1, "sha256Hash": AVAILABILITY_CALENDAR_HASH}}
    resp = client.get(
        f"{BASE_URL}/api/v3/PdpAvailabilityCalendarV2Query/{AVAILABILITY_CALENDAR_HASH}",
        params={
            "operationName": "PdpAvailabilityCalendarV2Query",
            "locale": "en",
            "currency": "USD",
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(extensions, separators=(",", ":")),
        },
        headers={
            "X-Airbnb-Api-Key": api_key,
            "X-Airbnb-Graphql-Platform": "web",
            "X-Airbnb-Graphql-Platform-Client": "minimalist-niobe",
            "X-Csrf-Without-Token": "1",
        },
    )
    resp.raise_for_status()
    try:
        calendar_months = resp.json()["data"]["node"]["pdpAvailabilityCalendar"]["calendarMonths"]
    except (KeyError, TypeError) as e:
        raise RuntimeError(
            f"Unexpected availability calendar response for room {room_id} — the "
            "persisted-query hash may be stale (see AVAILABILITY_CALENDAR_HASH)."
        ) from e

    days = []
    for month in calendar_months:
        for day in month.get("days", []):
            if day.get("calendarDate") and day["calendarDate"] >= today.isoformat():
                days.append(
                    {
                        "date": day["calendarDate"],
                        "booked": not day.get("available"),
                        "min_nights": day.get("minNights"),
                    }
                )
    return days


def get_room_location(client: httpx.Client, room_id: str) -> tuple[float, float] | None:
    """Fetches a public room page and pulls its lat/lng out of embedded page
    data. Used to get our own listings' coordinates (for comp-distance
    filtering) without needing the host dashboard.
    """
    resp = client.get(f"{BASE_URL}/rooms/{room_id}")
    resp.raise_for_status()
    lat_match = re.search(r'"latitude":(-?\d+\.\d+)', resp.text)
    lng_match = re.search(r'"longitude":(-?\d+\.\d+)', resp.text)
    if not lat_match or not lng_match:
        return None
    return float(lat_match.group(1)), float(lng_match.group(1))


def _decode_listing_id(global_id: str | None) -> str | None:
    """demandStayListing.id is a Relay-style global id, e.g. base64 of
    "DemandStayListing:39820889" — the numeric suffix is the same id used in
    /rooms/<id> URLs.
    """
    if not global_id:
        return None
    try:
        decoded = base64.b64decode(global_id).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded.rsplit(":", 1)[-1] if ":" in decoded else None


def _parse_display_price(structured_price: dict | None) -> float | None:
    """Prices come back as a formatted total-stay string (e.g. "$1,044"), not
    a nightly rate — good enough for logging/filtering, not for per-night math.
    """
    if not structured_price:
        return None
    primary = structured_price.get("primaryLine") or {}
    text = primary.get("discountedPrice") or primary.get("price")
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    return float(digits) if digits else None
