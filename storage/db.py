import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "pricing.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def insert_comp_snapshot(conn, scraped_at, comp_listing_id, date, price, available, min_nights, source):
    conn.execute(
        """INSERT INTO comp_snapshots
           (scraped_at, comp_listing_id, date, price, available, min_nights, source)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (scraped_at, comp_listing_id, date, price, available, min_nights, source),
    )


def insert_own_calendar(conn, scraped_at, listing_id, date, price, booked, min_nights):
    conn.execute(
        """INSERT INTO own_calendar
           (scraped_at, listing_id, date, price, booked, min_nights)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (scraped_at, listing_id, date, price, booked, min_nights),
    )


def insert_recommendation(conn, run_at, listing_id, date, recommended_price, prior_price, rationale, signals_json):
    conn.execute(
        """INSERT INTO recommendations
           (run_at, listing_id, date, recommended_price, prior_price, rationale, signals_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (run_at, listing_id, date, recommended_price, prior_price, rationale, signals_json),
    )


def latest_comp_snapshots(conn, comp_listing_id, since_date):
    return conn.execute(
        """SELECT * FROM comp_snapshots
           WHERE comp_listing_id = ? AND date >= ?
           ORDER BY scraped_at DESC""",
        (comp_listing_id, since_date),
    ).fetchall()


def all_comp_snapshots(conn, since_date):
    return conn.execute(
        """SELECT cs.* FROM comp_snapshots cs
           JOIN (
               SELECT date, comp_listing_id, MAX(scraped_at) AS max_scraped_at
               FROM comp_snapshots
               WHERE date >= ?
               GROUP BY date, comp_listing_id
           ) latest ON cs.date = latest.date
               AND cs.comp_listing_id = latest.comp_listing_id
               AND cs.scraped_at = latest.max_scraped_at
           WHERE cs.date >= ?
           ORDER BY cs.scraped_at DESC""",
        (since_date, since_date),
    ).fetchall()


def last_known_own_prices(conn, listing_id, since_date) -> dict[str, float]:
    """Most recent non-null scraped price per date, for carrying forward when
    a newer calendar scrape has no price (e.g. the public availability API).
    """
    rows = conn.execute(
        """SELECT date, price FROM own_calendar
           WHERE listing_id = ? AND date >= ? AND price IS NOT NULL
           ORDER BY scraped_at ASC""",
        (listing_id, since_date),
    ).fetchall()
    return {r["date"]: r["price"] for r in rows}


def latest_own_calendar(conn, listing_id, since_date):
    return conn.execute(
        """SELECT oc.* FROM own_calendar oc
           JOIN (
               SELECT date, MAX(scraped_at) AS max_scraped_at
               FROM own_calendar
               WHERE listing_id = ? AND date >= ?
               GROUP BY date
           ) latest ON oc.date = latest.date AND oc.scraped_at = latest.max_scraped_at
           WHERE oc.listing_id = ?
           ORDER BY oc.date ASC""",
        (listing_id, since_date, listing_id),
    ).fetchall()


def insert_availability_action(conn, applied_at, listing_id, date, action, detail, dry_run):
    conn.execute(
        """INSERT INTO availability_actions
           (applied_at, listing_id, date, action, detail, dry_run)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (applied_at, listing_id, date, action, detail, 1 if dry_run else 0),
    )


def agent_blocked_dates(conn, listing_id, since_date) -> set[str]:
    """Dates the Availability Agent currently holds blocked: those whose most
    recent real (non-dry-run) block/open action was a 'block'.

    Used to tell an agent-blocked night apart from a guest reservation, since
    the host calendar scrape reports both as `booked`.
    """
    rows = conn.execute(
        """SELECT aa.date, aa.action FROM availability_actions aa
           JOIN (
               SELECT date, MAX(applied_at) AS max_applied_at
               FROM availability_actions
               WHERE listing_id = ? AND date >= ? AND dry_run = 0
                 AND action IN ('block', 'open')
               GROUP BY date
           ) latest ON aa.date = latest.date AND aa.applied_at = latest.max_applied_at
           WHERE aa.listing_id = ? AND aa.dry_run = 0
             AND aa.action IN ('block', 'open')""",
        (listing_id, since_date, listing_id),
    ).fetchall()
    return {r["date"] for r in rows if r["action"] == "block"}


def last_applied_min_nights(conn, listing_id, since_date) -> dict[str, int]:
    """{date: min_nights} the agent last really applied, so a run can skip
    dates already at their target instead of reopening the multicalendar for
    every night in the horizon (each visit is a full page load).

    For 'min_nights' rows, `detail` holds the applied value as a plain int.
    """
    rows = conn.execute(
        """SELECT aa.date, aa.detail FROM availability_actions aa
           JOIN (
               SELECT date, MAX(applied_at) AS max_applied_at
               FROM availability_actions
               WHERE listing_id = ? AND date >= ? AND dry_run = 0 AND action = 'min_nights'
               GROUP BY date
           ) latest ON aa.date = latest.date AND aa.applied_at = latest.max_applied_at
           WHERE aa.listing_id = ? AND aa.dry_run = 0 AND aa.action = 'min_nights'""",
        (listing_id, since_date, listing_id),
    ).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["date"]] = int(str(r["detail"]).strip())
        except (TypeError, ValueError):
            continue  # older/malformed row: treat as unknown, re-apply
    return out
