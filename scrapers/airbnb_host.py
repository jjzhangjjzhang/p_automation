"""Authenticated scraping of your own Airbnb host dashboard: pricing calendar
and reservations for your own listings.

Because this logs in as you, it's normal logged-in browsing rather than
scraping someone else's data — lower ToS/detection risk than the public
search scraping in airbnb_public.py.

Login is NOT scripted here — run scripts/airbnb_host_login.py once to log in
manually in a real browser window (handles 2FA/OTP fine since a human does
it) and save the session to AUTH_STATE_PATH. HostSession just reuses that
saved session; if it's missing or expired, login() raises telling you to
rerun the login script.

Selectors below were captured from a live session on 2026-09-02 (see
data/debug/*.html dumps used to derive them) — Airbnb's dashboard DOM
changes periodically, so if this breaks, re-dump the pages and re-derive.
"""

import re
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "https://www.airbnb.com"
AUTH_STATE_PATH = Path(__file__).parent.parent / "data" / ".airbnb_auth_state.json"


def _dismiss_cookie_banner(page) -> None:
    """Removes the cookie-consent banner from the page if present, rather
    than trying to click an "Accept"-ish button -- its exact label isn't
    verified against the live site, whereas just deleting the element can't
    get that wrong, and it stops the banner from intercepting clicks on the
    real controls underneath it (seen blocking the Cancel/Save buttons)."""
    page.evaluate(
        "() => { const el = document.querySelector('[data-testid=\"main-cookies-banner-container\"]');"
        " if (el) el.remove(); }"
    )

# Candidate selectors for the day-panel controls, tried in order. Unlike the
# price input (verified against a live session), these were NOT captured from a
# live panel -- Airbnb's day panel varies by listing and build, so each control
# is a list and a total miss raises with the panel text rather than silently
# doing nothing. Re-derive with scripts/dump_host_pages.py if they all miss.
_AVAILABILITY_TOGGLES = [
    'input[data-testid="pna-availability-toggle"]',
    '[data-testid="pna-availability"] input[type="checkbox"]',
    '[data-testid="pna-availability"] [role="switch"]',
    '[role="dialog"] input[type="checkbox"][name*="avail" i]',
    '[role="dialog"] [role="switch"]',
]

_MIN_NIGHTS_INPUTS = [
    'input[data-testid="PriceInput-minNights"]',
    'input[data-testid="pna-min-nights"]',
    '[data-testid="pna-min-nights"] input',
    '[role="dialog"] input[name*="minNights" i]',
    '[role="dialog"] input[aria-label*="minimum" i]',
]


def _first_present(page, selectors: list[str]):
    """First selector in `selectors` that matches something, as a Locator."""
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            loc.wait_for(state="attached", timeout=1500)
        except Exception:
            continue
        if loc.count() > 0:
            return loc
    return None


def _toggle_is_on(toggle) -> bool:
    """True if a checkbox/switch reads as on. Checkboxes expose is_checked();
    ARIA switches only have aria-checked, so fall back to that."""
    try:
        return toggle.is_checked()
    except Exception:
        return str(toggle.get_attribute("aria-checked")).lower() == "true"


def _panel_text(page, panel) -> str:
    """Panel text for error messages -- best effort, never raises."""
    try:
        if panel.count() > 0:
            return panel.inner_text().replace("\n", " | ").strip()
        return page.inner_text("body")[:300]
    except Exception:
        return "<unreadable>"


_CALENDAR_COLLECT_JS = """() => {
    const cells = Array.from(document.querySelectorAll('button[data-date]'));
    return cells.map(b => {
        const cellDate = b.getAttribute('data-date');
        const describedBy = b.getAttribute('aria-describedby') || '';
        const booked = describedBy.startsWith('RESERVATION_');
        let price = null;
        if (!booked) {
            for (const s of b.querySelectorAll('span')) {
                const t = s.textContent.trim();
                if (/^\\$[\\d,]+(\\.\\d+)?$/.test(t)) { price = t; break; }
            }
        }
        return {date: cellDate, booked, price};
    });
}"""

_RESERVATION_COLUMNS = [
    "status", "guests", "contact", "checkin", "checkout",
    "booked_at", "listing", "confirmation_code", "total_payout", "actions",
]


class HostSession:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._listing_titles = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless, channel="chrome")
        storage_state = str(AUTH_STATE_PATH) if AUTH_STATE_PATH.exists() else None
        self._context = self._browser.new_context(storage_state=storage_state)
        return self

    def __exit__(self, *exc):
        if self._context:
            AUTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._context.storage_state(path=str(AUTH_STATE_PATH))
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _is_logged_in(self, page) -> bool:
        # Clicking the nav menu to check for "Log in or sign up" (an earlier
        # version of this method) is unreliable — a cookie-consent/privacy
        # modal can cover the page and intercept the click, causing a
        # timeout regardless of actual login state. Instead, hit a
        # host-only URL directly: Airbnb redirects unauthenticated
        # visitors to /login, which is a clean, click-free signal.
        page.goto(f"{BASE_URL}/hosting/listings", timeout=30000)
        page.wait_for_load_state("load", timeout=15000)
        return "/login" not in page.url

    def login(self):
        page = self._context.new_page()
        logged_in = AUTH_STATE_PATH.exists() and self._is_logged_in(page)
        page.close()
        if not logged_in:
            raise RuntimeError(
                "Not logged into Airbnb (or the saved session expired). "
                "Run `python scripts/airbnb_host_login.py` to log in manually, then retry."
            )

    def _get_listing_titles(self) -> dict[str, str]:
        """Maps listing_id -> public listing title, e.g.
        "1366459487284926886" -> "Entire 4BR house+game room+tree house+big backyard".

        Needed because the reservations table only shows the public title,
        not the nickname used in config/listings.yaml, and the
        listingId query param on /hosting/reservations is ignored by Airbnb
        (it always returns all listings' rows) — so filtering rows to one
        listing requires knowing its title.
        """
        if self._listing_titles is not None:
            return self._listing_titles
        page = self._context.new_page()
        page.goto(f"{BASE_URL}/hosting/listings", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        titles = {}
        for row in page.query_selector_all("tbody tr[data-testid]"):
            listing_id = row.get_attribute("data-testid")
            if not listing_id or not listing_id.isdigit():
                continue
            title = row.inner_text().strip().split("\n")[0]
            titles[listing_id] = title
        page.close()
        self._listing_titles = titles
        return titles

    def get_calendar(self, listing_id: str, months: int = 5) -> list[dict]:
        """Scrapes your host pricing calendar for one listing.

        Returns a list of dicts: {date, price, booked, min_nights}.
        min_nights is always None — it isn't exposed per-day on this view.
        """
        page = self._context.new_page()
        page.goto(f"{BASE_URL}/multicalendar/{listing_id}", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass  # the calendar polls in the background; networkidle rarely settles
        page.wait_for_timeout(2000)

        target_end = date.today() + timedelta(days=months * 31)
        collected: dict[str, dict] = {}
        max_date_seen = date.today()
        for _ in range(60):  # safety cap; each iteration covers a few days
            for row in page.evaluate(_CALENDAR_COLLECT_JS):
                collected[row["date"]] = row
            if collected:
                max_date_seen = max(max_date_seen, max(date.fromisoformat(d) for d in collected))
            if max_date_seen >= target_end:
                break
            page.evaluate(
                "() => { const el = document.querySelector('[data-testid=\"virtuoso-scroller\"]');"
                " if (el) el.scrollTop += 500; }"
            )
            page.wait_for_timeout(600)
        page.close()

        results = []
        for cell_date, row in sorted(collected.items()):
            price = _parse_price(row["price"]) if row["price"] else None
            results.append(
                {"date": cell_date, "price": price, "booked": row["booked"], "min_nights": None}
            )
        return results

    def _open_day_editor(self, listing_id: str, date_str: str):
        """Opens the multicalendar and clicks one day cell, returning the page
        with that day's edit panel open. Shared by set_price /
        set_availability / set_min_nights.
        """
        page = self._context.new_page()
        page.goto(f"{BASE_URL}/multicalendar/{listing_id}", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        _dismiss_cookie_banner(page)

        # Locators (not query_selector's static ElementHandles) so every
        # action re-queries the live DOM -- the calendar is a virtualized,
        # frequently-re-rendering list, and ElementHandle snapshots taken
        # before a re-render go stale and fail with "not attached to DOM".
        day_locator = page.locator(f'button[data-date="{date_str}"]').first
        for _ in range(60):  # safety cap; each iteration scrolls a few days into view
            if day_locator.count() > 0:
                break
            page.evaluate(
                "() => { const el = document.querySelector('[data-testid=\"virtuoso-scroller\"]');"
                " if (el) el.scrollTop += 500; }"
            )
            page.wait_for_timeout(600)
        if day_locator.count() == 0:
            page.close()
            raise ValueError(f"Date {date_str} not found on calendar (out of loaded range?)")
        day_locator.scroll_into_view_if_needed()
        day_locator.click()
        return page

    def _finish_edit(self, page, dry_run: bool) -> None:
        page.wait_for_timeout(300)
        _dismiss_cookie_banner(page)  # can reappear and block Cancel/Save
        page.get_by_role("button", name="Cancel" if dry_run else "Save").click()
        page.wait_for_timeout(1500)

    def set_price(self, listing_id: str, date_str: str, price: float, dry_run: bool = False) -> dict:
        """Sets the nightly price for one date on the host pricing calendar.

        Returns {date, previous_price, new_price, applied}. With dry_run=True,
        opens the edit panel and reads the current price but clicks Cancel
        instead of Save — nothing is changed on Airbnb.
        """
        page = self._open_day_editor(listing_id, date_str)

        price_box = page.locator('[data-testid="pna-price"]').first
        try:
            price_box.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        if price_box.count() == 0:
            page.close()
            raise RuntimeError(
                f"Price edit panel didn't open for {date_str} — likely booked/blocked, can't set a price."
            )
        price_box.click()

        price_input = page.locator('input[data-testid="PriceInput-nightlyPrice"]').first
        for attempt in range(2):  # the panel's first click sometimes doesn't land while it's still animating in
            try:
                price_input.wait_for(state="visible", timeout=8000)
            except Exception:
                pass
            if price_input.count() > 0:
                break
            price_box.click()
        if price_input.count() == 0:
            page.close()
            raise RuntimeError(f"Price input never appeared for {date_str}.")
        previous_price = price_input.get_attribute("value")

        price_input.click()
        price_input.fill(str(int(round(price))))
        self._finish_edit(page, dry_run)
        page.close()

        return {
            "date": date_str,
            "previous_price": _parse_price(previous_price) if previous_price else None,
            "new_price": price,
            "applied": not dry_run,
        }

    def set_availability(self, listing_id: str, date_str: str, available: bool, dry_run: bool = False) -> dict:
        """Opens or blocks one date on the host calendar.

        Returns {date, previous_available, new_available, applied, changed}.
        A date that is already in the requested state is left alone and
        returns changed=False. Refuses to touch a date held by a guest
        reservation -- that panel has no availability control, and this
        raises rather than guessing.

        NOTE: the selectors below are the fragile part. Airbnb reskins the
        day panel regularly; each control is tried as a list of candidates and
        a total miss raises with the panel's text so you can re-derive the
        selector with scripts/dump_host_pages.py.
        """
        page = self._open_day_editor(listing_id, date_str)
        panel = page.locator('[data-testid="pna-panel"], [role="dialog"]').first
        try:
            panel.wait_for(state="visible", timeout=8000)
        except Exception:
            pass

        toggle = _first_present(page, _AVAILABILITY_TOGGLES)
        if toggle is None:
            text = _panel_text(page, panel)
            page.close()
            raise RuntimeError(
                f"No availability control in the panel for {date_str} "
                f"(guest-reserved, or the UI changed). Panel text: {text[:300]!r}"
            )

        previous_available = _toggle_is_on(toggle)
        if previous_available == available:
            page.get_by_role("button", name="Cancel").click()
            page.wait_for_timeout(500)
            page.close()
            return {
                "date": date_str,
                "previous_available": previous_available,
                "new_available": available,
                "applied": False,
                "changed": False,
            }

        toggle.click()
        self._finish_edit(page, dry_run)
        page.close()
        return {
            "date": date_str,
            "previous_available": previous_available,
            "new_available": available,
            "applied": not dry_run,
            "changed": True,
        }

    def set_min_nights(self, listing_id: str, date_str: str, min_nights: int, dry_run: bool = False) -> dict:
        """Sets the minimum-stay length for stays checking in on one date.

        Returns {date, previous_min_nights, new_min_nights, applied, changed}.
        Same selector caveat as set_availability.
        """
        page = self._open_day_editor(listing_id, date_str)
        panel = page.locator('[data-testid="pna-panel"], [role="dialog"]').first
        try:
            panel.wait_for(state="visible", timeout=8000)
        except Exception:
            pass

        # The min-nights field often sits behind a collapsed "Stay length" /
        # "Availability" section -- expand it if it's there before looking.
        for name in ("Stay length", "Minimum stay", "Availability"):
            section = page.get_by_role("button", name=name)
            if section.count() > 0:
                try:
                    section.first.click()
                    page.wait_for_timeout(400)
                except Exception:
                    pass
                break

        field = _first_present(page, _MIN_NIGHTS_INPUTS)
        if field is None:
            text = _panel_text(page, panel)
            page.close()
            raise RuntimeError(
                f"No minimum-stay input in the panel for {date_str} "
                f"(guest-reserved, or the UI changed). Panel text: {text[:300]!r}"
            )

        raw_previous = field.get_attribute("value")
        try:
            previous_min_nights = int(str(raw_previous).strip())
        except (TypeError, ValueError):
            previous_min_nights = None

        if previous_min_nights == int(min_nights):
            page.get_by_role("button", name="Cancel").click()
            page.wait_for_timeout(500)
            page.close()
            return {
                "date": date_str,
                "previous_min_nights": previous_min_nights,
                "new_min_nights": int(min_nights),
                "applied": False,
                "changed": False,
            }

        field.click()
        field.fill(str(int(min_nights)))
        self._finish_edit(page, dry_run)
        page.close()
        return {
            "date": date_str,
            "previous_min_nights": previous_min_nights,
            "new_min_nights": int(min_nights),
            "applied": not dry_run,
            "changed": True,
        }

    def get_reservations(self, listing_id: str) -> list[dict]:
        """Scrapes reservations (all statuses) for one listing.

        Returns a list of dicts: {checkin, checkout, booked_at, nightly_rate, total_price}.
        Canceled reservations are excluded — they never actually occupied the
        calendar, so counting them would distort pace/lead-time history.
        """
        listing_title = self._get_listing_titles().get(listing_id)
        if listing_title is None:
            raise ValueError(f"Listing id {listing_id!r} not found on /hosting/listings")

        page = self._context.new_page()
        page.goto(f"{BASE_URL}/hosting/reservations/all", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(1000)

        results = []
        for row in page.query_selector_all('tr[data-testid="host-reservations-table-row"]'):
            status_el = row.query_selector('span[data-testid="host-reservations-status-color"]')
            status_key = status_el.get_attribute("data-status-key") if status_el else ""
            if status_key and "cancel" in status_key:
                continue

            cells = row.query_selector_all("td")
            values = {
                name: cells[i].inner_text().strip()
                for i, name in enumerate(_RESERVATION_COLUMNS)
                if i < len(cells)
            }
            if values.get("listing") != listing_title:
                continue

            checkin = _parse_host_date(values.get("checkin"))
            checkout = _parse_host_date(values.get("checkout"))
            booked_at = _parse_host_date(values.get("booked_at"))
            total_price = _parse_price(values.get("total_payout", ""))
            nightly_rate = None
            if total_price is not None and checkin and checkout:
                nights = (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days
                if nights > 0:
                    nightly_rate = round(total_price / nights, 2)

            results.append(
                {
                    "checkin": checkin,
                    "checkout": checkout,
                    "booked_at": booked_at,
                    "nightly_rate": nightly_rate,
                    "total_price": total_price,
                }
            )
        page.close()
        return results

    def get_all_reservations_detailed(self, max_pages: int = 40) -> list[dict]:
        """Scrapes every page of /hosting/reservations/all for all listings.

        Unlike get_reservations (first page only, trimmed fields), this walks
        the pagination and keeps the full detail needed for compliance
        reporting. Canceled rows are included with their status.

        Returns dicts: {listing_id, listing_title, confirmation_code, guest_name,
        num_guests, status, checkin, checkout, booked_at, host_payout}.
        listing_id is None if the row's title matches no listing.
        """
        title_to_id = {t: i for i, t in self._get_listing_titles().items()}
        page = self._context.new_page()
        page.goto(f"{BASE_URL}/hosting/reservations/all", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        results: list[dict] = []
        seen_codes: set[str] = set()
        for _ in range(max_pages):
            page_codes = []
            for row in page.query_selector_all('tr[data-testid="host-reservations-table-row"]'):
                parsed = _parse_detailed_row(row, title_to_id)
                if parsed is None:
                    continue
                page_codes.append(parsed["confirmation_code"])
                if parsed["confirmation_code"] not in seen_codes:
                    seen_codes.add(parsed["confirmation_code"])
                    results.append(parsed)

            next_btn = page.query_selector('button[aria-label="Next"]')
            if next_btn is None or next_btn.is_disabled():
                break
            first_before = page_codes[0] if page_codes else None
            next_btn.click()
            for _ in range(20):  # wait up to ~10s for the table to swap
                page.wait_for_timeout(500)
                row = page.query_selector('tr[data-testid="host-reservations-table-row"]')
                if row is not None:
                    cells = row.query_selector_all("td")
                    if len(cells) > 7 and cells[7].inner_text().strip() != first_before:
                        break
            else:
                break  # table never changed; avoid looping forever
        page.close()
        return results


def _parse_detailed_row(row, title_to_id: dict[str, str]) -> dict | None:
    cells = row.query_selector_all("td")
    if len(cells) < 9:
        return None
    values = {
        name: cells[i].inner_text().strip()
        for i, name in enumerate(_RESERVATION_COLUMNS)
        if i < len(cells)
    }
    code = values.get("confirmation_code")
    if not code:
        return None
    status_el = row.query_selector('span[data-testid="host-reservations-status-color"]')
    status_key = (status_el.get_attribute("data-status-key") if status_el else "") or ""
    status = values.get("status", "").split("\n")[0].strip()
    lines = [ln.strip() for ln in values.get("guests", "").split("\n") if ln.strip()]
    guest_name = lines[0] if lines else ""
    party_text = " ".join(lines[1:])
    # Adults + children (Airbnb's capacity count); infants/pets stay in `party`.
    num_guests = sum(int(n) for n in re.findall(r"(\d+)\s+(?:adult|child)", party_text))
    title = values.get("listing", "")
    return {
        "listing_id": title_to_id.get(title),
        "listing_title": title,
        "confirmation_code": code,
        "guest_name": guest_name,
        "num_guests": num_guests,
        "party": party_text,
        "status": "Canceled" if "cancel" in status_key.lower() else status,
        "checkin": _parse_host_date(values.get("checkin")),
        "checkout": _parse_host_date(values.get("checkout")),
        "booked_at": _parse_host_date(values.get("booked_at")),
        "host_payout": _parse_price(values.get("total_payout", "")),
    }


def _parse_price(text: str) -> float | None:
    digits = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    return float(digits) if digits else None


def _parse_host_date(text: str | None) -> str | None:
    """Parses the dashboard's "Sep 1, 2026" (optionally followed by a time
    line, e.g. "Mar 22, 2026\\n2:25 PM PT") into an ISO date string."""
    if not text:
        return None
    first_line = text.strip().split("\n")[0]
    match = re.match(r"[A-Za-z]{3} \d{1,2}, \d{4}", first_line)
    if not match:
        return None
    return datetime.strptime(match.group(), "%b %d, %Y").date().isoformat()
