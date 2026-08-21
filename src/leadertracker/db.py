"""SQLite schema and connection helper."""

import sqlite3

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS traders (
    address TEXT PRIMARY KEY,
    account_value REAL,
    is_active INTEGER DEFAULT 1,
    added_at INTEGER,
    removed_at INTEGER,
    last_fill_time INTEGER,
    -- Account-wide 30d volume from the leaderboard's `month` window, cached
    -- at cohort-refresh time. The poller reads it to decide whether a
    -- below-floor wallet is dormant enough to retire.
    vlm_30d REAL,
    -- BTC realized PnL over the rank window at the time the wallet was last
    -- selected. Kept for auditing why a wallet is in the cohort.
    btc_pnl REAL
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

-- OHLC for the price-level ladder's ATR band. Only high/low/close are kept:
-- true range needs no open, and nothing else reads candles.
--
-- The newest row is always the CURRENTLY FORMING candle, whose high/low/close
-- still move. That's deliberate - today's range belongs in the ATR - and the
-- primary key makes it self-correcting: every poll re-inserts the same
-- open_time with INSERT OR REPLACE and the row converges as the day closes.
CREATE TABLE IF NOT EXISTS candles (
    coin TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (coin, interval, open_time)
);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS is a
# no-op on an existing table, so these have to be applied by hand.
_ADDED_COLUMNS = {
    "traders": [
        ("vlm_30d", "REAL"),
        ("btc_pnl", "REAL"),
    ],
}


def migrate(conn: sqlite3.Connection) -> list:
    """Add any missing columns to an existing database. Returns what it added."""
    applied = []
    for table, columns in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                applied.append(f"{table}.{name}")
    if applied:
        conn.commit()
    return applied


def init(path: str = DB_PATH) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    migrate(conn)
    return conn


if __name__ == "__main__":
    init()
    print(f"Initialized schema at {DB_PATH}")
