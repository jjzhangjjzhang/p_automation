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
        self._browser = self._playwright.chromium.launch(headless=self.headless)
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

    def set_price(self, listing_id: str, date_str: str, price: float, dry_run: bool = False) -> dict:
        """Sets the nightly price for one date on the host pricing calendar.

        Returns {date, previous_price, new_price, applied}. With dry_run=True,
        opens the edit panel and reads the current price but clicks Cancel
        instead of Save — nothing is changed on Airbnb.
        """
        page = self._context.new_page()
        page.goto(f"{BASE_URL}/multicalendar/{listing_id}", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        day_btn = page.query_selector(f'button[data-date="{date_str}"]')
        for _ in range(60):  # safety cap; each iteration scrolls a few days into view
            if day_btn is not None:
                break
            page.evaluate(
                "() => { const el = document.querySelector('[data-testid=\"virtuoso-scroller\"]');"
                " if (el) el.scrollTop += 500; }"
            )
            page.wait_for_timeout(600)
            day_btn = page.query_selector(f'button[data-date="{date_str}"]')
        if day_btn is None:
            page.close()
            raise ValueError(f"Date {date_str} not found on calendar (out of loaded range?)")
        day_btn.scroll_into_view_if_needed()
        day_btn.click()
        page.wait_for_timeout(1000)

        price_box = page.query_selector('[data-testid="pna-price"]')
        if price_box is None:
            page.close()
            raise RuntimeError(
                f"Price edit panel didn't open for {date_str} — likely booked/blocked, can't set a price."
            )
        price_box.click()
        try:
            page.wait_for_selector('input[data-testid="PriceInput-nightlyPrice"]', timeout=5000)
        except Exception:
            pass
        price_input = page.query_selector('input[data-testid="PriceInput-nightlyPrice"]')
        if price_input is None:
            page.close()
            raise RuntimeError(f"Price input never appeared for {date_str}.")
        previous_price = price_input.get_attribute("value")

        price_input.click()
        price_input.fill("")
        price_input.type(str(int(round(price))))
        page.wait_for_timeout(300)

        if dry_run:
            page.get_by_role("button", name="Cancel").click()
        else:
            page.get_by_role("button", name="Save").click()
        page.wait_for_timeout(1500)
        page.close()

        return {
            "date": date_str,
            "previous_price": _parse_price(previous_price) if previous_price else None,
            "new_price": price,
            "applied": not dry_run,
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
