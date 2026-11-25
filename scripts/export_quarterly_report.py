"""Exports Airbnb reservations overlapping a date window to a CSV for the
San Diego County quarterly occupancy report.

Usage: python scripts/export_quarterly_report.py [--start 2026-08-01] [--end 2026-10-31] [--out outputs]

Nights are clipped to the window; the checkout night is not counted.
"""

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml  # noqa: E402
from scrapers.airbnb_host import HostSession  # noqa: E402

ROOT = Path(__file__).parent.parent
COLUMNS = [
    "property", "confirmation_code", "guest_name", "num_guests", "party", "status",
    "checkin", "checkout", "total_nights", "nights_in_quarter", "host_payout",
]


def nights_in_window(checkin: str, checkout: str, start: date, end: date) -> int:
    """Occupied nights in [start, end] inclusive; checkout day is not a night."""
    ci, co = date.fromisoformat(checkin), date.fromisoformat(checkout)
    lo, hi = max(ci, start), min(co, date.fromordinal(end.toordinal() + 1))
    return max((hi - lo).days, 0)


def build_rows(reservations: list[dict], listings: list[dict], start: date, end: date) -> list[dict]:
    names = {l["id"]: l["name"] for l in listings}
    rows = []
    for r in reservations:
        if r["listing_id"] not in names or not r["checkin"] or not r["checkout"]:
            continue
        in_window = nights_in_window(r["checkin"], r["checkout"], start, end)
        if in_window == 0:
            continue
        canceled = r["status"].lower().startswith("cancel")
        total = (date.fromisoformat(r["checkout"]) - date.fromisoformat(r["checkin"])).days
        rows.append({
            "property": names[r["listing_id"]],
            "confirmation_code": r["confirmation_code"],
            "guest_name": r["guest_name"],
            "num_guests": r["num_guests"],
            "party": r["party"],
            "status": r["status"],
            "checkin": r["checkin"],
            "checkout": r["checkout"],
            "total_nights": total,
            "nights_in_quarter": 0 if canceled else in_window,
            "host_payout": "" if r["host_payout"] is None else f"{r['host_payout']:.2f}",
        })
    return sorted(rows, key=lambda x: (x["property"], x["checkin"], x["confirmation_code"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default="2026-10-31")
    ap.add_argument("--out", default=str(ROOT / "outputs"))
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    listings = yaml.safe_load((ROOT / "config" / "listings.yaml").read_text())["listings"]

    with HostSession(headless=True) as session:
        try:
            session.login()
        except RuntimeError as e:
            sys.exit(f"{e}")
        reservations = session.get_all_reservations_detailed()
    print(f"Scraped {len(reservations)} reservations total")

    rows = build_rows(reservations, listings, start, end)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"county_occupancy_{start}_to_{end}.csv"
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    for l in listings:
        mine = [r for r in rows if r["property"] == l["name"]]
        active = [r for r in mine if r["nights_in_quarter"] > 0]
        print(f"{l['name']}: {len(active)} stays, {sum(r['nights_in_quarter'] for r in active)} "
              f"nights occupied ({len(mine) - len(active)} canceled)")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
