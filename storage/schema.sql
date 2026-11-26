CREATE TABLE IF NOT EXISTS comp_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scraped_at TEXT NOT NULL,
    comp_listing_id TEXT NOT NULL,
    date TEXT NOT NULL,
    price REAL,
    available INTEGER,
    min_nights INTEGER,
    source TEXT NOT NULL -- 'seed' or 'auto_discover'
);

CREATE TABLE IF NOT EXISTS own_calendar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scraped_at TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    date TEXT NOT NULL,
    price REAL,
    booked INTEGER,
    min_nights INTEGER
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    checkin TEXT NOT NULL,
    checkout TEXT NOT NULL,
    booked_at TEXT,
    nightly_rate REAL,
    total_price REAL
);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    date TEXT NOT NULL,
    recommended_price REAL,
    prior_price REAL,
    rationale TEXT,
    signals_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_comp_snapshots_date ON comp_snapshots(comp_listing_id, date);
CREATE INDEX IF NOT EXISTS idx_own_calendar_date ON own_calendar(listing_id, date);
CREATE INDEX IF NOT EXISTS idx_recommendations_date ON recommendations(listing_id, date);

-- What the Availability Agent actually changed on the Airbnb calendar.
-- Needed for more than an audit trail: the host calendar scrape reports a
-- single `booked` flag and can't tell a guest reservation from a night the
-- agent blocked itself. Without this log, an agent-blocked night would read
-- as "booked" on the next run, which both strands that night and makes it
-- look like a booking boundary to the gap detector. apply_availability.py
-- reads the agent's own open blocks back and un-marks them before planning.
CREATE TABLE IF NOT EXISTS availability_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    applied_at TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    date TEXT NOT NULL,
    action TEXT NOT NULL, -- 'block' | 'open' | 'min_nights'
    detail TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_availability_actions_date
    ON availability_actions(listing_id, date, applied_at);
