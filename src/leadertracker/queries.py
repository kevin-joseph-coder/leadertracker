"""Read-side queries: current open positions and the hourly flow series.

Both are computed at read time - no materialized aggregates table for the
MVP. Add one only if the hourly query actually gets slow.
"""

import time

from .config import COIN, DEFAULT_HOURS

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
