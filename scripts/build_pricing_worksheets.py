"""Builds one pricing worksheet (.csv) per listing: for each of
your unbooked nights in the next `comps.price_lookup_days` days, shows
competitor pricing (high/median/low/avail_n/lowest-priced comp) alongside
your current price and the latest pipeline recommendation, with a blank
new_price column you can fill in and apply with scripts/apply_prices_from_csv.py.

Assumes pipeline.py has already been run today (for own_calendar, comp
pricing, and a recommendations file) — this script only reads from the DB
and outputs/recommendations, it doesn't scrape anything itself.

Usage:
    python scripts/build_pricing_worksheets.py
"""

import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).parent.parent))
from storage import db  # noqa: E402

CONFIG_PATH = Path(__file__).parent.parent / "config" / "listings.yaml"
RECOMMENDATIONS_DIR = Path(__file__).parent.parent / "outputs" / "recommendations"
OUTPUT_DIR = Path(__file__).parent.parent / "outputs"

HEADER = [
    "listing_name", "listing_id", "date", "day", "avail_n", "high", "median", "low",
    "current", "suggested", "delta", "note", "lowest_listing_url", "new_price",
]


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_latest_recommendations() -> dict[tuple[str, str], dict]:
    files = sorted(RECOMMENDATIONS_DIR.glob("*.json"))
    if not files:
        return {}
    with open(files[-1]) as f:
        decisions = json.load(f)
    return {(d["listing_id"], d["date"]): d for d in decisions}


def _comp_stats_by_date(comp_snapshots: list[dict]) -> dict[str, dict]:
    by_date: dict[str, list[tuple[float, str]]] = {}
    for row in comp_snapshots:
        if row.get("price") is None or not row.get("available"):
            continue
        by_date.setdefault(row["date"], []).append((row["price"], row["comp_listing_id"]))

    stats = {}
    for d, entries in by_date.items():
        prices = [p for p, _ in entries]
        lowest_price, lowest_id = min(entries, key=lambda e: e[0])
        stats[d] = {
            "avail_n": len(entries),
            "high": max(prices),
            "median": sorted(prices)[len(prices) // 2] if len(prices) % 2 else (sorted(prices)[len(prices) // 2 - 1] + sorted(prices)[len(prices) // 2]) / 2,
            "low": min(prices),
            "lowest_listing_url": f"https://www.airbnb.com/rooms/{lowest_id}",
        }
    return stats


def build_rows(listing: dict, config: dict, conn, comp_stats: dict, recs: dict) -> list[list]:
    guardrails = config["guardrails"]
    horizon = date.today() + timedelta(days=config["comps"]["price_lookup_days"])
    max_change_pct = guardrails["max_daily_change_pct"]

    rows = []
    for row in db.latest_own_calendar(conn, listing["id"], date.today().isoformat()):
        d = row["date"]
        if not d or row["booked"] or date.fromisoformat(d) > horizon:
            continue

        current = row["price"]
        stats = comp_stats.get(d, {})
        rec = recs.get((listing["id"], d))
        suggested = rec["recommended_price"] if rec else None
        delta = round(suggested - current, 2) if suggested is not None and current is not None else None
        note = ""
        if delta is not None and current:
            if abs(delta) >= current * (max_change_pct / 100) - 0.5:
                note = f">{max_change_pct:.0f}%/day clamp"

        rows.append([
            listing["name"], listing["id"], d, date.fromisoformat(d).strftime("%a"),
            stats.get("avail_n", 0), stats.get("high"), stats.get("median"), stats.get("low"),
            current, suggested, delta, note, stats.get("lowest_listing_url"), None,
        ])
    return rows


def write_worksheet(path: Path, rows: list[list]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(rows)


HIGHLIGHT_FILLS = {
    "low": "BDD7EE",      # light blue
    "current": "FFE699",  # light amber
}


def write_xlsx(path: Path, rows: list[list]) -> None:
    """Same data as the CSV, with the `low` and `current` columns color-coded
    (CSV can't carry formatting). The CSV stays the input for
    apply_prices_from_csv.py.
    """
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for row in rows:
        ws.append(row)

    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for col_name, color in HIGHLIGHT_FILLS.items():
        col_idx = HEADER.index(col_name) + 1
        fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
        for r in range(1, ws.max_row + 1):
            ws.cell(row=r, column=col_idx).fill = fill
    for idx, name in enumerate(HEADER, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = 40 if name == "lowest_listing_url" else max(10, len(name) + 2)
    wb.save(path)


def main():
    config = load_config()
    conn = db.get_conn()
    recs = load_latest_recommendations()
    comp_snapshots = [dict(r) for r in db.all_comp_snapshots(conn, date.today().isoformat())]
    comp_stats = _comp_stats_by_date(comp_snapshots)

    today_str = date.today().isoformat()
    for listing in config["listings"]:
        rows = build_rows(listing, config, conn, comp_stats, recs)
        slug = listing["name"].lower().replace(" ", "_")
        out_path = OUTPUT_DIR / f"pricing_worksheet_{today_str}_{slug}.csv"
        write_worksheet(out_path, rows)
        print(f"Wrote {len(rows)} unbooked nights for {listing['name']} to {out_path}")

    conn.close()


if __name__ == "__main__":
    main()
