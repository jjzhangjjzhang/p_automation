"""One-off tool: attaches to a Chrome instance running with
--remote-debugging-port=9222, drives it to an Airbnb search and a listing
page, and prints every GraphQL persisted-query hash it observes so we can
refresh STAYS_SEARCH_HASH / CALENDAR_HASH in scrapers/airbnb_public.py.
"""

import json
import re
import time
import urllib.parse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

SEARCH_URL = (
    "https://www.airbnb.com/s/San-Diego--CA-92122/homes"
    "?checkin=2026-12-20&checkout=2026-12-21&adults=1"
)
LISTING_URL = "https://www.airbnb.com/rooms/1645836922268604771"

captured = []


def on_request(request):
    parsed = urllib.parse.urlparse(request.url)
    if "airbnb.com" not in parsed.netloc:
        return
    qs = urllib.parse.parse_qs(parsed.query)
    if "operationName" not in qs and "extensions" not in qs:
        return
    op_name = qs.get("operationName", ["?"])[0]
    sha = None
    # StaysSearch (and some others) carry the hash in the URL path:
    # /api/v3/<Operation>/<hash>
    path_match = re.search(r"/api/v3/[^/]+/([0-9a-f]{64})", parsed.path)
    if path_match:
        sha = path_match.group(1)
    if sha is None and "extensions" in qs:
        try:
            ext = json.loads(qs["extensions"][0])
            sha = ext.get("persistedQuery", {}).get("sha256Hash")
        except (json.JSONDecodeError, KeyError):
            pass
    captured.append((op_name, sha, request.url))


def safe_goto(page, url, label):
    print(f"Navigating to {label}: {url}")
    try:
        page.goto(url, timeout=30000)
    except PlaywrightError as e:
        print(f"(goto warning: {e})")
    try:
        page.wait_for_load_state("load", timeout=15000)
    except PlaywrightError as e:
        print(f"(load-state warning: {e})")
    time.sleep(4)  # let async XHRs fire after initial load


with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    context = browser.contexts[0]
    context.on("request", on_request)

    page = context.new_page()
    safe_goto(page, SEARCH_URL, "search")
    try:
        page.mouse.wheel(0, 2000)
    except PlaywrightError as e:
        print(f"(scroll warning: {e})")
    time.sleep(5)
    # The first page of results is server-rendered; StaysSearch only fires
    # on a follow-up interaction such as pagination.
    try:
        page.mouse.wheel(0, 20000)
        time.sleep(2)
        page.click('a[aria-label="Next"]', timeout=5000)
        time.sleep(5)
    except PlaywrightError as e:
        print(f"(could not click next page: {e})")
    safe_goto(page, LISTING_URL, "listing")

    # Try to trigger the availability calendar by clicking the date selector.
    try:
        page.click('[data-testid="change-dates-checkIn"]', timeout=5000)
        time.sleep(3)
    except PlaywrightError as e:
        print(f"(could not click date selector: {e})")

    page.close()

print("\n--- Captured operations ---")
seen = set()
for op_name, sha, url in captured:
    key = (op_name, sha)
    if key in seen:
        continue
    seen.add(key)
    print(f"{op_name}: sha256Hash={sha}")

print("\n--- Relevant hashes ---")
for op_name, sha, url in captured:
    if op_name in ("StaysSearch", "PdpAvailabilityCalendar") and sha:
        print(f"{op_name} -> {sha}")
