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
