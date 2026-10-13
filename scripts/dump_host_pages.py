"""Ad-hoc debug tool: saves the rendered HTML + a screenshot of the host
pricing-calendar and reservations pages so real selectors can be worked out
without needing to open devtools manually. Not part of the pipeline.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml  # noqa: E402
from scrapers.airbnb_host import BASE_URL, HostSession  # noqa: E402

OUT_DIR = Path(__file__).parent.parent / "data" / "debug"
OUT_DIR.mkdir(parents=True, exist_ok=True)

config = yaml.safe_load((Path(__file__).parent.parent / "config" / "listings.yaml").read_text())
listing = config["listings"][0]
listing_id = listing["id"]

with HostSession(headless=True) as session:
    session.login()
    page = session._context.new_page()

    page.goto(f"{BASE_URL}/hosting/listings/editor/{listing_id}/pricing/calendar")
    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)
    (OUT_DIR / "calendar.html").write_text(page.content())
    page.screenshot(path=str(OUT_DIR / "calendar.png"), full_page=True)
    print("saved calendar.html + calendar.png, url:", page.url)

    page.goto(f"{BASE_URL}/hosting/reservations?listingId={listing_id}")
    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)
    (OUT_DIR / "reservations.html").write_text(page.content())
    page.screenshot(path=str(OUT_DIR / "reservations.png"), full_page=True)
    print("saved reservations.html + reservations.png, url:", page.url)

    page.close()
