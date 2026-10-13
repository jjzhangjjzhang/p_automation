"""Daily orchestrator: runs the 5-agent pipeline end to end and writes a
recommendations file. Intended to run once per day via cron.

    Booking Pace ─────┐
    Competitor Agent ──┼──> Revenue Optimizer ──> LLM Decision Agent
    Calendar Agent ────┘

Booking Pace runs first because the Competitor Agent now needs your own
unbooked dates (from the own_calendar table) to know which nights to fetch
comp pricing for — see agents/competitor_agent.py.
"""

import json
import os
from datetime import date, datetime

import yaml
from dotenv import load_dotenv

from agents import booking_pace_agent, calendar_agent, competitor_agent, decision_agent, revenue_optimizer
from storage import db

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "listings.yaml")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "outputs", "recommendations")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def main():
    load_dotenv()
    config = load_config()
    db.init_db()
    conn = db.get_conn()

    print("Running Booking Pace Agent...")
    pace_by_listing = booking_pace_agent.run(config, conn)

    print("Running Competitor Agent...")
    competitor_agent.run(config, conn)

    guardrails = config["guardrails"]
    since_date = date.today().isoformat()

    print("Running Calendar Agent + Revenue Optimizer...")
    shortlists_by_listing = {}
    gap_nights_by_listing = {}
    at_risk_nights_by_listing = {}
    signals_by_listing = {}

    comp_snapshots = [dict(r) for r in db.all_comp_snapshots(conn, since_date)]

    for listing in config["listings"]:
        listing_id = listing["id"]
        own_calendar_rows = [dict(r) for r in db.latest_own_calendar(conn, listing_id, since_date)]

        gap_nights = calendar_agent.find_gap_nights(own_calendar_rows)
        gap_nights_by_listing[listing_id] = gap_nights
        at_risk_nights = calendar_agent.find_at_risk_nights(own_calendar_rows)
        at_risk_nights_by_listing[listing_id] = at_risk_nights

        shortlists_by_listing[listing_id] = revenue_optimizer.score_listing(
            listing_id,
            own_calendar_rows,
            comp_snapshots,
            pace_by_listing.get(listing_id, 1.0),
            gap_nights,
            guardrails,
            at_risk_nights,
        )

    shortlists_by_listing = revenue_optimizer.coordinate_twin_listings(
        shortlists_by_listing, pace_by_listing, guardrails["min_price_gap_between_listings"], guardrails
    )

    for listing in config["listings"]:
        listing_id = listing["id"]
        signals_by_listing[listing_id] = {
            "pace_ratio": pace_by_listing.get(listing_id, 1.0),
            "gap_nights": sorted(gap_nights_by_listing.get(listing_id, [])),
            "at_risk_nights": at_risk_nights_by_listing.get(listing_id, {}),
            "shortlists": shortlists_by_listing.get(listing_id, {}),
        }

    print("Running LLM Decision Agent...")
    decisions = decision_agent.run(signals_by_listing, guardrails)

    run_at = datetime.now().isoformat()
    for d in decisions:
        db.insert_recommendation(
            conn,
            run_at=run_at,
            listing_id=d["listing_id"],
            date=d["date"],
            recommended_price=d["recommended_price"],
            prior_price=None,
            rationale=d["rationale"],
            signals_json=json.dumps(signals_by_listing.get(d["listing_id"], {})),
        )
    conn.commit()
    conn.close()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{date.today().isoformat()}.json")
    with open(out_path, "w") as f:
        json.dump(decisions, f, indent=2)
    print(f"Wrote {len(decisions)} recommendations to {out_path}")


if __name__ == "__main__":
    main()
