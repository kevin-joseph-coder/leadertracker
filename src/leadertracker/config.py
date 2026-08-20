"""Shared configuration for the Hyperliquid BTC tracker MVP."""

import os

# This file is src/leadertracker/config.py, so the repo root is three levels
# up. Everything else derives from it, which keeps the paths correct whether
# the package is imported from a source checkout or an editable install.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Runtime state that is not served to the browser.
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

DB_PATH = os.path.join(DATA_DIR, "tracker.db")

# The ranked cohort dump. Diagnostic output, not read by the page.
COHORT_PATH = os.path.join(DATA_DIR, "cohort.json")

# Where positions.json / hourly.json are written for the frontend to read.
# This is web/, NOT data/, and deliberately so: index.html fetches them as
# same-directory relative URLs, so they must sit beside the page for one
# static file server to cover everything.
WEB_DIR = os.path.join(BASE_DIR, "web")

# Created on import so a fresh clone can run the pipeline with no setup step.
for _d in (DATA_DIR, LOG_DIR, WEB_DIR):
    os.makedirs(_d, exist_ok=True)

# --- Cohort selection ---
# Overall envelope of the tiers below. MIN_ACCOUNT_VALUE is also the poller's
# retirement equity floor, which is why it stays a standalone constant.
MIN_ACCOUNT_VALUE = 100_000.0
MAX_ACCOUNT_VALUE = 5_000_000.0
TOP_N = 100
RANK_WINDOW = "week"  # "day" | "week" | "month" | "allTime"  (week = 7d)

# The prefilter is tiered by account size because no single metric is fair
# across the whole band. Ranking everyone by dollar PnL lets the $5M accounts
# crowd out the $100k ones on sheer size; ranking everyone by ROI does the
# reverse, since a 50% week on $100k beats any realistic return on $5M.
# Splitting the band lets each tier compete against its own size class.
#
# Bounds are [min, max) - a wallet is shortlisted by exactly one tier, and
# $1M lands in the upper one. `pool` is that tier's candidate budget.
#
# The pools are lopsided on purpose, from measured marginal yield in the
# 2026-08-19 run (500 + 100 candidates):
#
#   depth 1-100    $100k-$1M   31 eligible per 100
#   depth 100-500  $100k-$1M   ~10 per 100, flat
#   depth 501-600  $1M-$5M     25 per 100
#
# The lower band decays to ~10% after its first hundred and stays there, so
# buying depth in it is expensive. The upper band returns 25% and was being
# sampled at 100 of 1,709 wallets - 6% of the band - which is where the
# untapped BTC traders actually were. The extra budget therefore goes almost
# entirely upstairs: 900 of 1,709 is ~53% coverage.
#
# Expect the 25% to decay with depth the way the lower tier's 31% did; 10-15%
# at depth 900 would still beat the lower tier's tail.
COHORT_TIERS = (
    {"min": 100_000.0, "max": 1_000_000.0, "metric": "pnl", "pool": 600},
    {"min": 1_000_000.0, "max": 5_000_000.0, "metric": "roi", "pool": 900},
)

# The final cohort is ranked by BTC realized PnL, which can only be computed
# from a wallet's fills - roughly 2 paged /info calls each. Scoring all ~10k
# wallets in the account-value band would take hours, so the leaderboard's
# account-wide tier metrics are used only to shortlist this many candidates
# in total, which are then enriched and re-ranked on BTC alone.
#
# Measured cost is ~4.5s per candidate end to end (600 candidates in 45m18s
# on 2026-08-19) - the two paged fills calls at FILL_CALL_DELAY plus the sleep
# after them, plus a cheap userRole call. Rejected roles skip the fills and
# cost only POSITION_CALL_DELAY, which is why the average lands under the
# 4.75s worst case.
#
# At 1500 candidates that is ~1h50m. Two things depend on that number and
# will not adjust themselves:
#
#   - scheduled_task.xml sets ExecutionTimeLimit. It must exceed the run with
#     margin for 429 retries; cohort.py writes cohort.json and the traders
#     table only at the very end, so a hard-terminate at the limit discards
#     the entire run. Currently PT4H.
#   - apilock makes the poller SKIP cycles for the whole refresh, so a
#     ~1h50m run leaves a ~2h hole in position_snapshots. No fill data is
#     lost (the watermark catches up), but the Positioning column reads null
#     for those hours. This is the real ceiling on pool depth, not wall time.
#
# Derived from the tiers so the total and the per-tier budgets can't drift.
CANDIDATE_POOL = sum(t["pool"] for t in COHORT_TIERS)

# Only wallets whose userRole is in this set are eligible. Vaults, agents and
# sub-accounts trade on someone else's behalf, so their positions don't
# represent an individual trader's conviction.
ALLOWED_ROLES = ("user",)

# Lookback for the BTC enrichment pass. Matches RANK_WINDOW = "week" so the
# BTC score covers the same period the leaderboard prefilter ranked on.
BTC_RANK_LOOKBACK_MS = 7 * 24 * 60 * 60 * 1000

# --- Cohort retention ---
# A wallet is retired ONLY when it is both below the equity floor and
# effectively dormant. Falling out of the ranked top N is explicitly NOT a
# reason to retire - the cohort is a tracked panel, not a live leaderboard
# mirror, and churning it would shred the position history the charts read.
#
# 30d volume comes from the leaderboard's `month` window, cached on the
# traders row at cohort-refresh time. It is account-wide (not BTC-only) and
# only as fresh as the last cohort.py run.
RETIRE_VLM_30D_FLOOR = 100_000.0

# Never let the active panel shrink below this. If retiring a wallet would
# cross the line, it stays active regardless of equity or volume.
MIN_COHORT_SIZE = 100

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
