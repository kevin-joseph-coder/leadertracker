"""SQLite schema and connection helper."""

import sqlite3

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS traders (
    address TEXT PRIMARY KEY,
    account_value REAL,
    is_active INTEGER DEFAULT 1,
    added_at INTEGER,
    removed_at INTEGER,
    last_fill_time INTEGER
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    coin TEXT NOT NULL,
    dir TEXT NOT NULL,
    px REAL NOT NULL,
    sz REAL NOT NULL,
    notional_usd REAL NOT NULL,
    fill_time INTEGER NOT NULL,
    closed_pnl REAL,
    ingested_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills(fill_time);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    coin TEXT NOT NULL,
    signed_size REAL NOT NULL,
    entry_px REAL,
    unrealized_pnl REAL,
    position_value_usd REAL,
    account_value_usd REAL,
    poll_time INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON position_snapshots(poll_time);
-- The open-positions read is MAX(poll_time) GROUP BY address; this keeps it
-- fast as snapshots accumulate (~28k rows/day at a 5-minute cycle).
CREATE INDEX IF NOT EXISTS idx_snapshots_addr_time
    ON position_snapshots(address, poll_time);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init(path: str = DB_PATH) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


if __name__ == "__main__":
    init()
    print(f"Initialized schema at {DB_PATH}")
