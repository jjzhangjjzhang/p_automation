"""Reads outputs/pricing_worksheet_*.csv and pushes any filled-in new_price
values to Airbnb's host pricing calendar.

Defaults to a dry run (opens each day's edit panel, reads the price
currently shown there, and reports what it would change to — then clicks
Cancel instead of Save, so nothing on Airbnb actually changes). Pass
--apply to actually save the changes.

Usage:
    python scripts/apply_prices_from_csv.py outputs/pricing_worksheet_2026-09-02.csv
    python scripts/apply_prices_from_csv.py outputs/pricing_worksheet_2026-09-02.csv --apply
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.airbnb_host import HostSession  # noqa: E402
from scrapers.cache import polite_delay  # noqa: E402


def main():
    args = sys.argv[1:]
    apply = "--apply" in args
    csv_path = next(a for a in args if not a.startswith("--"))

    rows_to_apply = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            new_price = row.get("new_price", "").strip()
            if new_price:
                rows_to_apply.append((row["listing_name"], row["listing_id"], row["date"], float(new_price)))

    if not rows_to_apply:
        print("No rows with a new_price filled in — nothing to do.")
        return

    mode = "APPLY (writing to Airbnb)" if apply else "DRY RUN (no changes will be saved)"
    print(f"{mode} — {len(rows_to_apply)} price(s) to process\n")

    with HostSession(headless=True) as session:
        session.login()
        for listing_name, listing_id, date_str, new_price in rows_to_apply:
            try:
                result = session.set_price(listing_id, date_str, new_price, dry_run=not apply)
                status = "APPLIED" if result["applied"] else "would apply"
                print(
                    f"[{listing_name}] {date_str}: ${result['previous_price']:.0f} -> "
                    f"${result['new_price']:.0f} ({status})"
                )
            except Exception as e:
                print(f"[{listing_name}] {date_str}: FAILED — {e!r}")
            polite_delay([2, 4])


if __name__ == "__main__":
    main()
