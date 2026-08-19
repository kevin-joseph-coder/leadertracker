"""Shared configuration for the Hyperliquid BTC tracker MVP."""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(BASE_DIR, "tracker.db")

# Where positions.json / hourly.json are written for the frontend to read.
# Same directory as index.html so one static file server covers everything.
WEB_DIR = BASE_DIR

# --- Cohort selection ---
MIN_ACCOUNT_VALUE = 100_000.0
MAX_ACCOUNT_VALUE = 1_000_000.0
TOP_N = 100
RANK_WINDOW = "week"  # "day" | "week" | "month" | "allTime"  (week = 7d)
RANK_METRIC = "pnl"   # "pnl" or "roi"

# --- Tracked asset ---
COIN = "BTC"

# --- Polling ---
POLL_INTERVAL_SECONDS = 300          # one full cycle every 5 minutes

# Hyperliquid /info is IP rate limited by request *weight* (~1200/min).
# clearinghouseState costs 2, userFillsByTime costs 20 + more per 20 items.
# 100 wallets => 200 weight for positions, >=2000 for fills, so the fill
# loop has to be spread across more than a minute. These delays keep a full
# cycle at roughly 25s + 120s, comfortably inside a 300s interval.
POSITION_CALL_DELAY = 0.25
FILL_CALL_DELAY = 1.2

# On a wallet's first ever fill poll, look back this far.
FIRST_RUN_LOOKBACK_MS = 60 * 60 * 1000  # 1 hour

# Re-request a small overlap before the last seen fill so nothing is missed
# at the boundary. Duplicates are dropped by INSERT OR IGNORE on fill_id.
FILL_OVERLAP_MS = 60 * 1000  # 1 minute

# --- Frontend series window ---
DEFAULT_HOURS = 168
