"""Applies the Availability Agent's minimum-stay and availability plan to the
Airbnb host calendar.

Dry run by default: it computes the plan, reads the live value out of each
day's edit panel, prints what it WOULD change, and clicks Cancel. Pass
--apply to actually save.

    python scripts/apply_availability.py                 # dry run, all listings
    python scripts/apply_availability.py --apply
    python scripts/apply_availability.py --listing Cascade --apply
    python scripts/apply_availability.py --plan-only      # no browser at all

Reads the latest own_calendar snapshot from the DB, so run pipeline.py (or at
least the Booking Pace Agent) first -- this script does not scrape the
calendar itself.

## Scheduling

Availability for a `turnover_toggle` single-night gap depends on the clock, so
this must run at BOTH ends of the window or the night gets stuck in whichever
state it was last left in. With Cascade's 17:00-07:00 window:

    0 7 * * *   cd /path/to/p_automation && .venv/bin/python scripts/apply_availability.py --apply
    0 17 * * *  cd /path/to/p_automation && .venv/bin/python scripts/apply_availability.py --apply

Each run opens a browser session, so keep it to the two edges plus the daily
pipeline rather than polling every few minutes.
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents import availability_agent  # noqa: E402
from storage import db  # noqa: E402

CONFIG_PATH = Path(__file__).parent.parent / "config" / "listings.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def plan_for_listing(conn, listing: dict, now: datetime, days: int) -> tuple[dict, set[str], dict]:
    """(plan, agent_blocked) for one listing.

    Nights the agent itself blocked on an earlier run come back from the scrape
    as `booked`. They're un-marked here before planning, for two reasons: the
    night itself must stay a planning candidate so it can be reopened, and
    leaving it marked would make the gap detector treat it as a booking
    boundary and mis-size the surrounding gaps.
    """
    rows = [dict(r) for r in db.latest_own_calendar(conn, listing["id"], now.date().isoformat())]
    agent_blocked = db.agent_blocked_dates(conn, listing["id"], now.date().isoformat())
    for row in rows:
        if row["date"] in agent_blocked:
            row["booked"] = 0
    plan = availability_agent.plan_listing(rows, listing["stay_rules"], now)
    horizon = (now.date() + timedelta(days=days)).isoformat()
    plan = {d: p for d, p in plan.items() if d <= horizon}
    applied_min = db.last_applied_min_nights(conn, listing["id"], now.date().isoformat())
    return plan, agent_blocked, applied_min


def describe(listing_name: str, date_str: str, plan: dict, currently_blocked: bool) -> str:
    state = "available" if plan["available"] else "BLOCKED"
    was = "blocked" if currently_blocked else "available"
    line = f"  {listing_name} {date_str}  min={plan['min_nights']}  {was} -> {state}"
    if plan["reasons"]:
        line += f"\n      why: {'; '.join(plan['reasons'])}"
    if plan["flags"]:
        line += f"\n      flag: {'; '.join(plan['flags'])}"
    return line


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually save changes (default: dry run)")
    parser.add_argument("--plan-only", action="store_true", help="print the plan and exit; never opens a browser")
    parser.add_argument("--listing", help="limit to one listing by name (e.g. Cascade)")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="show the browser; useful when the panel selectors need re-deriving")
    parser.add_argument("--days", type=int, default=60,
                        help="how many days ahead to manage (default: 60)")
    parser.add_argument("--limit", type=int,
                        help="stop after this many dates needing a change (safety valve)")
    parser.add_argument("--skip-min-nights", action="store_true",
                        help="only touch availability, leave minimum-stay alone")
    args = parser.parse_args()

    config = load_config()
    now = datetime.now()
    # Creates the schema on a fresh checkout, and adds availability_actions to
    # a pricing.db that predates it (every statement is CREATE ... IF NOT EXISTS).
    db.init_db()
    conn = db.get_conn()

    listings = [l for l in config["listings"] if l.get("stay_rules")]
    skipped = [l["name"] for l in config["listings"] if not l.get("stay_rules")]
    if args.listing:
        listings = [l for l in listings if l["name"].lower() == args.listing.lower()]
        if not listings:
            print(f"No listing named {args.listing!r} with a stay_rules block. "
                  f"Configured: {[l['name'] for l in config['listings']]}")
            return 1
    if skipped and not args.listing:
        print(f"Skipping (no stay_rules in config/listings.yaml): {', '.join(skipped)}")
    if not listings:
        print("No listings have a stay_rules block -- nothing to do.")
        return 1

    print(f"Planning as of {now.isoformat(timespec='minutes')}")
    work = {}
    for listing in listings:
        plan, agent_blocked, applied_min = plan_for_listing(conn, listing, now, args.days)
        if not plan:
            print(f"  {listing['name']}: no calendar rows in the DB -- run pipeline.py first")
            continue
        changes = []
        for date_str, p in sorted(plan.items()):
            currently_blocked = date_str in agent_blocked
            needs_availability = p["available"] == currently_blocked  # mismatch
            # Unknown (never applied) counts as needing a write: the host
            # calendar doesn't expose per-day min-nights to the scraper, so the
            # first run has to open each panel once to learn the real value.
            needs_min_nights = (
                not args.skip_min_nights and applied_min.get(date_str) != p["min_nights"]
            )
            if needs_availability or needs_min_nights:
                changes.append((date_str, p, currently_blocked, needs_availability, needs_min_nights))

        if args.limit:
            changes = changes[: args.limit]
        work[listing["id"]] = (listing, changes)

        print(f"\n{listing['name']}: {len(plan)} unbooked nights in the next {args.days} days, "
              f"{len(changes)} needing a calendar write")
        for date_str, p, currently_blocked, _na, _nm in changes:
            print(describe(listing["name"], date_str, p, currently_blocked))

    if args.plan_only:
        conn.close()
        return 0

    from scrapers.airbnb_host import HostSession
    from scrapers.cache import polite_delay

    mode = "APPLYING" if args.apply else "DRY RUN (nothing will be saved)"
    print(f"\n{mode} -- opening Airbnb host calendar")
    applied_at = datetime.now().isoformat()
    errors = 0

    with HostSession(headless=args.headless) as session:
        # Validates the saved session up front: raises a clear "re-run
        # airbnb_host_login.py" instead of letting every date fail with an
        # inscrutable missing-selector error.
        session.login()
        for listing, changes in work.values():
            for date_str, p, currently_blocked, needs_availability, needs_min_nights in changes:
                touched = False
                if needs_availability:
                    try:
                        result = session.set_availability(
                            listing["id"], date_str, p["available"], dry_run=not args.apply
                        )
                        touched = True
                        if result["changed"]:
                            print(f"  {listing['name']} {date_str}: availability -> "
                                  f"{'available' if p['available'] else 'blocked'}"
                                  f"{'' if args.apply else ' (dry run)'}")
                            if args.apply:
                                db.insert_availability_action(
                                    conn, applied_at, listing["id"], date_str,
                                    "open" if p["available"] else "block",
                                    "; ".join(p["reasons"]) or None, dry_run=False,
                                )
                    except Exception as e:
                        errors += 1
                        touched = True
                        print(f"  !! {listing['name']} {date_str}: availability failed: {e}")

                if needs_min_nights:
                    try:
                        result = session.set_min_nights(
                            listing["id"], date_str, p["min_nights"], dry_run=not args.apply
                        )
                        touched = True
                        if result["changed"]:
                            print(f"  {listing['name']} {date_str}: min_nights "
                                  f"{result['previous_min_nights']} -> {p['min_nights']}"
                                  f"{'' if args.apply else ' (dry run)'}")
                        # Logged even when the panel already showed the target:
                        # the log is this script's only record of per-day
                        # min-nights (the scraper can't read it), so skipping
                        # the no-change case would make every later run reopen
                        # this date's panel to rediscover the same value.
                        if args.apply:
                            db.insert_availability_action(
                                conn, applied_at, listing["id"], date_str, "min_nights",
                                str(p["min_nights"]), dry_run=False,
                            )
                    except Exception as e:
                        errors += 1
                        touched = True
                        print(f"  !! {listing['name']} {date_str}: min_nights failed: {e}")

                if touched:
                    polite_delay([2, 4])

    conn.commit()
    conn.close()
    if errors:
        print(f"\n{errors} date(s) failed. If every date failed with a selector error, the day-panel "
              f"DOM has changed -- rerun with --no-headless and re-derive the selectors in "
              f"scrapers/airbnb_host.py (_AVAILABILITY_TOGGLES / _MIN_NIGHTS_INPUTS).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
