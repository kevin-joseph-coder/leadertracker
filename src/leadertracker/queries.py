"""Read-side queries: open positions, the hourly flow series, the price ladder.

All computed at read time - no materialized aggregates table for the MVP.
Add one only if a query actually gets slow.
"""

import math
import time

from .config import (
    ATR_BAND_MULT,
    ATR_INTERVAL,
    ATR_PERIOD,
    COIN,
    DEFAULT_HOURS,
    LEVEL_BUCKET_PCT,
)

# Latest snapshot per active trader. The poller writes a zero-size row when
# a wallet has no BTC position, so "latest row, size != 0" correctly drops
# wallets that have since gone flat rather than showing a stale position.
OPEN_POSITIONS_SQL = """
SELECT s.address, s.coin, s.signed_size, s.entry_px, s.unrealized_pnl,
       s.position_value_usd, s.account_value_usd, s.poll_time
  FROM position_snapshots s
  JOIN traders t
    ON t.address = s.address AND t.is_active = 1
  JOIN (SELECT address, MAX(poll_time) AS max_poll
          FROM position_snapshots
         WHERE coin = ?
         GROUP BY address) latest
    ON latest.address = s.address AND latest.max_poll = s.poll_time
 WHERE s.coin = ?
   AND s.signed_size != 0
 ORDER BY ABS(s.position_value_usd) DESC
"""

# Net BTC exposure held by the cohort, as of the end of each hour.
#
# Positions are state, not flow: the poller snapshots every active wallet
# each cycle (~12 times an hour), so an hour's value is the LAST snapshot
# taken for each wallet inside that hour, summed across wallets. Longs count
# positive, shorts negative; positionValue itself is unsigned, so the sign
# comes from signed_size.
#
# Deliberately not filtered on is_active: snapshots are only ever written
# for wallets active at the time, so each hour already contains exactly the
# wallets being tracked then. Filtering on the current flag instead would
# retroactively rewrite past hours every time a wallet drops out.
POSITIONING_SQL = """
WITH last_in_hour AS (
    SELECT address,
           (poll_time / 3600000) * 3600000 AS hour_bucket,
           MAX(poll_time) AS last_poll
      FROM position_snapshots
     WHERE coin = ?
       AND poll_time >= ?
     GROUP BY address, hour_bucket
)
SELECT l.hour_bucket,
       SUM(CASE WHEN s.signed_size > 0 THEN  s.position_value_usd
                WHEN s.signed_size < 0 THEN -s.position_value_usd
                ELSE 0 END)                                        AS net_position_usd,
       SUM(CASE WHEN s.signed_size > 0 THEN s.position_value_usd
                ELSE 0 END)                                        AS long_position_usd,
       SUM(CASE WHEN s.signed_size < 0 THEN s.position_value_usd
                ELSE 0 END)                                        AS short_position_usd,
       SUM(CASE WHEN s.signed_size != 0 THEN 1 ELSE 0 END)         AS wallets_with_position
  FROM last_in_hour l
  JOIN position_snapshots s
    ON s.address = l.address
   AND s.poll_time = l.last_poll
   AND s.coin = ?
 GROUP BY l.hour_bucket
"""

HOURLY_SQL = """
SELECT
    (fill_time / 3600000) * 3600000 AS hour_bucket,
    SUM(CASE WHEN dir = 'Open Long'   THEN notional_usd ELSE 0 END) AS net_long_opened,
    SUM(CASE WHEN dir = 'Open Short'  THEN notional_usd ELSE 0 END) AS net_short_opened,
    SUM(CASE WHEN dir = 'Close Long'  THEN notional_usd ELSE 0 END) AS closed_long,
    SUM(CASE WHEN dir = 'Close Short' THEN notional_usd ELSE 0 END) AS closed_short,
    COUNT(*) AS fill_count
FROM fills
WHERE coin = ?
  AND fill_time >= ?
GROUP BY hour_bucket
ORDER BY hour_bucket
"""


def open_positions(conn, coin: str = COIN) -> dict:
    """Latest open position per active trader, plus an aggregate summary."""
    rows = [dict(r) for r in conn.execute(OPEN_POSITIONS_SQL, (coin, coin))]

    long_notional = sum(r["position_value_usd"] or 0 for r in rows if r["signed_size"] > 0)
    short_notional = sum(r["position_value_usd"] or 0 for r in rows if r["signed_size"] < 0)
    long_wallets = sum(1 for r in rows if r["signed_size"] > 0)
    short_wallets = sum(1 for r in rows if r["signed_size"] < 0)

    active = conn.execute(
        "SELECT COUNT(*) AS n FROM traders WHERE is_active = 1"
    ).fetchone()["n"]

    return {
        "coin": coin,
        "generated_at": int(time.time() * 1000),
        "summary": {
            "total_long_notional_usd": long_notional,
            "total_short_notional_usd": short_notional,
            "net_notional_usd": long_notional - short_notional,
            "long_wallets": long_wallets,
            "short_wallets": short_wallets,
            "wallets_with_position": len(rows),
            "active_wallets": active,
            "total_unrealized_pnl_usd": sum(r["unrealized_pnl"] or 0 for r in rows),
        },
        "positions": rows,
    }


# --------------------------------------------------------------------------
# Price-level ladder
# --------------------------------------------------------------------------

# Newest candles last, which is the order wilder_atr() wants. The inner LIMIT
# takes them off the recent end; the outer ORDER BY puts them back in time
# order. The table keeps accumulating, so this must not read all of history.
RECENT_CANDLES_SQL = """
SELECT * FROM (
    SELECT open_time, high, low, close
      FROM candles
     WHERE coin = ? AND interval = ?
     ORDER BY open_time DESC
     LIMIT ?
) ORDER BY open_time
"""


def wilder_atr(candles: list, period: int = ATR_PERIOD):
    """Wilder's Average True Range. Candles oldest first; None if too few.

    True range is max(high-low, |high-prev_close|, |low-prev_close|) - the
    last two terms are what make it capture overnight gaps that the bare
    high-low range misses. The first candle has no previous close, so N
    candles yield N-1 true ranges and period+1 candles are the minimum.

    Wilder smoothing, not a simple moving average: seed on the mean of the
    first `period` ranges, then ATR = (prev*(period-1) + tr) / period. It
    decays old ranges instead of dropping them off a cliff, so the band
    doesn't jump the day a big range ages out of the window.
    """
    if period < 1 or len(candles) < period + 1:
        return None

    trs = []
    for prev, cur in zip(candles, candles[1:]):
        prev_close = prev["close"]
        trs.append(max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev_close),
            abs(cur["low"] - prev_close),
        ))

    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def mark_price(rows: list):
    """Current mark, derived from the positions themselves. None if unknowable.

    `positionValue` is Hyperliquid's mark-based notional, so for any row
    position_value_usd / abs(signed_size) IS the mark at that poll. No price
    endpoint needed, and the number is synchronised with the instant the
    positions were observed - which is what the ladder wants, rather than a
    live tick taken minutes later.

    Two defences against bad rows. Only rows from the newest poll_time count:
    a wallet whose clearinghouseState call failed keeps its previous row, and
    that row carries an older mark. And the estimate is a notional-weighted
    MEDIAN rather than a mean, so a single surviving oddity moves the answer
    by nothing instead of by its share of the total.
    """
    usable = [
        r for r in rows
        if r["signed_size"] and r["position_value_usd"] and r["poll_time"]
    ]
    if not usable:
        return None

    newest = max(r["poll_time"] for r in usable)
    marks = sorted(
        (r["position_value_usd"] / abs(r["signed_size"]), r["position_value_usd"])
        for r in usable if r["poll_time"] == newest
    )
    if not marks:
        return None

    half = sum(w for _, w in marks) / 2
    seen = 0.0
    for px, w in marks:
        seen += w
        if seen >= half:
            return px
    return marks[-1][0]


def price_levels(conn, coin: str = COIN) -> dict:
    """Long vs short entry notional, bucketed by entry price, around the mark.

    Every open position carries entry_px - the wallet's AVERAGE entry for that
    position - so grouping on it says where the cohort's book actually sits
    rather than just how big it is.

    Two things to know about the numbers:

      - Value is ENTRY notional, abs(size) * entry_px, not the mark notional
        the summary tiles use. It is the money committed at that level, which
        is the quantity the level is about.
      - Only positions inside the band are counted, so these totals are a
        subset and will NOT reconcile with the tiles. The page says so.

    Returns None if the mark or the ATR can't be established (an empty cohort,
    or a database with no candles polled yet).
    """
    rows = [dict(r) for r in conn.execute(OPEN_POSITIONS_SQL, (coin, coin))]

    candles = [dict(r) for r in conn.execute(
        RECENT_CANDLES_SQL, (coin, ATR_INTERVAL, max(ATR_PERIOD * 4, 60)))]
    atr = wilder_atr(candles)

    mark = mark_price(rows)
    source = "snapshot"
    if mark is None and candles:
        # Nobody in the cohort holds the coin, so there is no position to read
        # a mark off. The last close is stale by up to a day but still beats
        # showing nothing.
        mark, source = candles[-1]["close"], "candle"

    if mark is None or not mark or atr is None or atr <= 0:
        return None

    step = mark * LEVEL_BUCKET_PCT
    reach = ATR_BAND_MULT * atr
    band_low, band_high = mark - reach, mark + reach

    # Anchoring the grid on the mark puts the current price exactly on the
    # boundary between level -1 and level 0, so the "current" marker always
    # lands between two rows instead of cutting one in half.
    idx_of = lambda px: math.floor((px - mark) / step)
    lo_idx, hi_idx = idx_of(band_low), idx_of(band_high)

    # Every index in range, not just the occupied ones: an empty level is a
    # fact about the book, and gaps would make the ladder misread as compact.
    levels = {
        i: {
            "level_px": mark + (i + 0.5) * step,
            "level_low": mark + i * step,
            "level_high": mark + (i + 1) * step,
            "long_notional_usd": 0.0, "short_notional_usd": 0.0,
            "long_wallets": 0, "short_wallets": 0,
        }
        for i in range(lo_idx, hi_idx + 1)
    }

    no_entry = out_of_band = 0
    for r in rows:
        px, size = r["entry_px"], r["signed_size"]
        # entry_px is nullable. Such a position has no place on the ladder,
        # but it is reported rather than silently dropped.
        if not px or not size:
            no_entry += 1
            continue
        if px < band_low or px > band_high:
            out_of_band += 1
            continue

        lvl = levels[idx_of(px)]
        side = "long" if size > 0 else "short"
        lvl[f"{side}_notional_usd"] += abs(size) * px
        lvl[f"{side}_wallets"] += 1

    # High price first, so the ladder reads top-down like an order book.
    ordered = [levels[i] for i in sorted(levels, reverse=True)]

    return {
        "coin": coin,
        "mark_px": mark,
        "mark_source": source,
        "atr": atr,
        "atr_period": ATR_PERIOD,
        "atr_interval": ATR_INTERVAL,
        "atr_as_of": candles[-1]["open_time"] if candles else None,
        "band_mult": ATR_BAND_MULT,
        "band_low": band_low,
        "band_high": band_high,
        "bucket_pct": LEVEL_BUCKET_PCT,
        "bucket_usd": step,
        "in_band": {
            "long_notional_usd": sum(l["long_notional_usd"] for l in ordered),
            "short_notional_usd": sum(l["short_notional_usd"] for l in ordered),
            "long_wallets": sum(l["long_wallets"] for l in ordered),
            "short_wallets": sum(l["short_wallets"] for l in ordered),
            "positions": len(rows) - no_entry - out_of_band,
        },
        "excluded": {"out_of_band": out_of_band, "no_entry_px": no_entry},
        "rows": ordered,
    }


def hourly_aggregates(conn, hours: int = DEFAULT_HOURS, coin: str = COIN) -> dict:
    """Hourly BTC flow buckets, plus the cohort's net exposure each hour."""
    now_ms = int(time.time() * 1000)
    since = ((now_ms - hours * 3600_000) // 3600_000) * 3600_000

    buckets = {}
    for r in conn.execute(HOURLY_SQL, (coin, since)):
        buckets[r["hour_bucket"]] = dict(r)

    # An hour can have positions without any fills (nobody traded but the
    # cohort still held exposure), so merge on the union of both keys.
    for r in conn.execute(POSITIONING_SQL, (coin, since, coin)):
        b = buckets.setdefault(r["hour_bucket"], {
            "hour_bucket": r["hour_bucket"],
            "net_long_opened": 0.0, "net_short_opened": 0.0,
            "closed_long": 0.0, "closed_short": 0.0, "fill_count": 0,
        })
        b.update({k: r[k] for k in (
            "net_position_usd", "long_position_usd",
            "short_position_usd", "wallets_with_position")})

    # net_position_usd stays absent - never 0.0 - for hours with no snapshot,
    # so the page can tell "not observed" apart from a genuinely flat cohort.
    return {
        "coin": coin,
        "hours": hours,
        "generated_at": now_ms,
        "buckets": [buckets[k] for k in sorted(buckets)],
    }
