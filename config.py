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
# 10 minutes, not 5. Each wallet now needs TWO fills calls (TWAP slices are
# reported separately), which doubled the weight of the fill loop to ~4000+.
# At ~1200 weight/min that loop alone needs 3.5-6 minutes, so a 5-minute
# cycle cannot complete without tripping 429s. Nothing is lost by polling
# less often: the watermark plus overlap still captures every fill, and the
# page buckets positions by hour regardless.
POLL_INTERVAL_SECONDS = 600

# Hyperliquid /info is IP rate limited by request *weight* (~1200/min).
# clearinghouseState costs 2; the two fills endpoints cost 20 each, plus
# more per 20 items returned. Each wallet needs BOTH fills calls, since
# TWAP slices are absent from userFillsByTime, so 100 wallets is ~200
# weight for positions and ~4000 for fills. At 1200/min the fill loop must
# span at least ~3.5 minutes, hence a delay after every individual call
# rather than per wallet.
#
# 1.5s per call = 40 weight per 3s = 800 weight/min, leaving headroom for
# the per-item surcharge. A 1.0s delay measured out at ~2400 weight/min and
# produced 429s in practice. ~300s of fills + ~25s of positions per cycle.
POSITION_CALL_DELAY = 0.25
FILL_CALL_DELAY = 1.5

# On a wallet's first ever fill poll, look back this far.
FIRST_RUN_LOOKBACK_MS = 60 * 60 * 1000  # 1 hour

# Re-request a small overlap before the last seen fill so nothing is missed
# at the boundary. Duplicates are dropped by INSERT OR IGNORE on fill_id.
FILL_OVERLAP_MS = 60 * 1000  # 1 minute

# --- Frontend series window ---
DEFAULT_HOURS = 168
