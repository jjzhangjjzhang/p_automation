"""Ad-hoc smoke test for scrapers/airbnb_host.py — checks saved session validity
and prints what get_calendar/get_reservations currently return for one listing.
Not part of the pipeline; safe to delete after selectors are filled in.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml  # noqa: E402
from scrapers.airbnb_host import HostSession  # noqa: E402

config = yaml.safe_load((Path(__file__).parent.parent / "config" / "listings.yaml").read_text())
listing = config["listings"][0]
listing_id = listing["id"]

print(f"Testing listing: {listing['name']} ({listing_id})")

with HostSession(headless=True) as session:
    session.login()
    print("Login check: OK (session valid)")

    calendar = session.get_calendar(listing_id, months=1)
    print(f"get_calendar returned {len(calendar)} rows")
    if calendar:
        print("sample:", calendar[:3])

    reservations = session.get_reservations(listing_id)
    print(f"get_reservations returned {len(reservations)} rows")
    if reservations:
        print("sample:", reservations[:3])
