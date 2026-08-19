"""The pipeline: poll positions, poll fills, dump JSON for the frontend.

Run: python poller.py              # loop forever
     python poller.py --once       # a single cycle, useful for testing
     python poller.py --positions  # just the position poller
     python poller.py --fills      # just the fill poller
     python poller.py --export     # just re-dump the JSON files

Cohort refresh is deliberately NOT in this loop - run `python cohort.py`
once a day (cron / Task Scheduler / by hand).
"""

import argparse
import json
import os
import sys
import time

import db
import hl
import queries
from classify import classify_fills, to_float
from config import (
    COIN,
    DEFAULT_HOURS,
    FILL_CALL_DELAY,
    FILL_OVERLAP_MS,
    FIRST_RUN_LOOKBACK_MS,
    MIN_ACCOUNT_VALUE,
    MIN_COHORT_SIZE,
    POLL_INTERVAL_SECONDS,
    POSITION_CALL_DELAY,
    RETIRE_VLM_30D_FLOOR,
    WEB_DIR,
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def active_traders(conn) -> list:
    return [
        (r["address"], r["last_fill_time"])
        for r in conn.execute(
            "SELECT address, last_fill_time FROM traders WHERE is_active = 1 ORDER BY address"
        )
    ]


def cached_vlm_30d(conn) -> dict:
    """address -> account-wide 30d volume, as of the last cohort.py run.

    NULL is preserved as None and means "never measured", NOT "zero volume".
    That distinction is load-bearing now that dormancy alone can retire a
    wallet: 70 of 119 wallets predate the vlm_30d column, and collapsing
    NULL to 0.0 would retire every one of them for lack of data rather than
    for lack of trading. Unknown volume never retires anyone; the wallet
    gets a real number at the next cohort.py refresh.
    """
    return {
        r["address"]: r["vlm_30d"]
        for r in conn.execute("SELECT address, vlm_30d FROM traders")
    }


# --------------------------------------------------------------------------
# Position poller
# --------------------------------------------------------------------------

def hl_total_account_value(address: str):
    """Total equity, or None if the lookup fails (treated as 'can't confirm')."""
    try:
        return hl.total_account_value(address)
    except hl.HLError:
        return None


def poll_positions(conn) -> dict:
    traders = active_traders(conn)
    vlm_30d = cached_vlm_30d(conn)
    stats = {"polled": 0, "with_position": 0, "deactivated": 0,
             "retired_dormant": 0, "held_offbook": 0, "held_floor": 0,
             "errors": 0}
    now_ms = int(time.time() * 1000)
    # Tracked across the loop so MIN_COHORT_SIZE is enforced against the live
    # count, not the count at the start of the cycle.
    active_count = len(traders)

    for address, _ in traders:
        try:
            state = hl.clearinghouse_state(address)
        except hl.HLError as e:
            stats["errors"] += 1
            log(f"  positions {address[:10]}... failed: {e}")
            time.sleep(POSITION_CALL_DELAY)
            continue

        account_value = to_float(state.get("marginSummary", {}).get("accountValue"))

        btc = None
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == COIN:
                btc = pos
                break

        if btc is not None:
            stats["with_position"] += 1
            signed_size = to_float(btc.get("szi"))
            entry_px = to_float(btc.get("entryPx"), None)
            unrealized = to_float(btc.get("unrealizedPnl"))
            position_value = to_float(btc.get("positionValue"))
        else:
            # Always write a row, even when flat. Without this, a wallet that
            # closed its position would keep showing its last open snapshot.
            signed_size, entry_px, unrealized, position_value = 0.0, None, 0.0, 0.0

        conn.execute(
            """INSERT INTO position_snapshots
                   (address, coin, signed_size, entry_px, unrealized_pnl,
                    position_value_usd, account_value_usd, poll_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (address, COIN, signed_size, entry_px, unrealized,
             position_value, account_value, now_ms),
        )

        # A wallet is retired if it is below the equity floor OR dormant -
        # either one is sufficient. Dropping out of the leaderboard's ranked
        # top N is still NOT grounds for removal; cohort.py never retires.
        #
        # Dormancy is judged on cached 30d volume, and UNKNOWN IS NOT DORMANT:
        # a NULL vlm_30d means the wallet predates the column, not that it
        # sat still. Retiring on missing data would have removed 70 of 119
        # wallets. They get a real number at the next cohort.py refresh.
        vlm = vlm_30d.get(address)
        dormant = vlm is not None and vlm <= RETIRE_VLM_30D_FLOOR

        # marginSummary.accountValue only covers the DEFAULT perp clearinghouse,
        # while the cohort's $100k floor comes from the leaderboard's TOTAL
        # equity (spot + every perp dex). Comparing them directly retired 35 of
        # 100 wallets on the first poll - e.g. one wallet reported perp 0.0
        # while holding $402k of spot USDC. So a wallet that looks under the
        # floor gets one confirming `portfolio` call, which reports the same
        # total the leaderboard does. Healthy wallets cost no extra call.
        total = None
        below_floor = False
        if account_value < MIN_ACCOUNT_VALUE:
            total = hl_total_account_value(address)
            if total is not None and total >= MIN_ACCOUNT_VALUE:
                # Equity lives outside the default perp clearinghouse.
                stats["held_offbook"] += 1
                conn.execute(
                    "UPDATE traders SET account_value = ? WHERE address = ?",
                    (total, address),
                )
            else:
                below_floor = True
            time.sleep(POSITION_CALL_DELAY)
        else:
            conn.execute(
                "UPDATE traders SET account_value = ? WHERE address = ?",
                (account_value, address),
            )

        if below_floor or dormant:
            if active_count <= MIN_COHORT_SIZE:
                # Would take the panel under its floor. Keep it and say so;
                # the fix is a cohort refresh, not a smaller panel.
                stats["held_floor"] += 1
            else:
                # Record the equity we actually retired on. Without this the
                # row keeps a stale account_value - one wallet retired at
                # $10k of real equity still read $815k from its cohort-time
                # leaderboard snapshot, making the decision look like a bug.
                # `total` is None when the wallet was retired for dormancy
                # alone, or when the confirming call failed; either way, keep
                # the last known value rather than overwriting it with nothing.
                if total is not None:
                    conn.execute(
                        """UPDATE traders
                              SET account_value = ?, is_active = 0, removed_at = ?
                            WHERE address = ?""",
                        (total, now_ms, address),
                    )
                else:
                    conn.execute(
                        "UPDATE traders SET is_active = 0, removed_at = ? WHERE address = ?",
                        (now_ms, address),
                    )
                stats["deactivated"] += 1
                stats["retired_dormant"] += 1 if dormant and not below_floor else 0
                active_count -= 1

        stats["polled"] += 1
        conn.commit()
        time.sleep(POSITION_CALL_DELAY)

    conn.commit()
    log(
        f"positions: {stats['polled']}/{len(traders)} polled, "
        f"{stats['with_position']} hold {COIN}, "
        f"{stats['deactivated']} retired "
        f"({stats['retired_dormant']} for dormancy alone), "
        f"{stats['held_offbook']} kept (equity outside default perp), "
        f"{stats['held_floor']} kept (would breach MIN_COHORT_SIZE), "
        f"{stats['errors']} errors"
    )
    return stats


# --------------------------------------------------------------------------
# Fill poller
# --------------------------------------------------------------------------

def poll_fills(conn, lookback_ms: int = None) -> dict:
    """Poll new fills for every active trader.

    lookback_ms forces a fixed window for every wallet, ignoring watermarks -
    used by --backfill to seed history. Re-ingesting is safe: INSERT OR IGNORE
    on fill_id makes it idempotent.
    """
    traders = active_traders(conn)
    stats = {"polled": 0, "fetched": 0, "btc_rows": 0, "inserted": 0, "errors": 0}
    now_ms = int(time.time() * 1000)

    for address, last_fill_time in traders:
        if lookback_ms is not None:
            start_time = now_ms - lookback_ms
        elif last_fill_time:
            # Re-request a small overlap; INSERT OR IGNORE makes it free.
            start_time = max(0, last_fill_time - FILL_OVERLAP_MS)
        else:
            start_time = now_ms - FIRST_RUN_LOOKBACK_MS

        try:
            # Both sources: TWAP child fills are absent from userFillsByTime.
            fills = hl.all_fills_by_time(address, start_time, delay=FILL_CALL_DELAY)
        except hl.HLError as e:
            stats["errors"] += 1
            log(f"  fills {address[:10]}... failed: {e}")
            time.sleep(FILL_CALL_DELAY)
            continue

        stats["fetched"] += len(fills)
        rows = classify_fills(address, fills, coin=COIN)
        stats["btc_rows"] += len(rows)

        if rows:
            before = conn.total_changes
            conn.executemany(
                """INSERT OR IGNORE INTO fills
                       (fill_id, address, coin, dir, px, sz, notional_usd,
                        fill_time, closed_pnl, ingested_at)
                   VALUES (:fill_id, :address, :coin, :dir, :px, :sz,
                           :notional_usd, :fill_time, :closed_pnl, :ingested_at)""",
                [dict(r, ingested_at=now_ms) for r in rows],
            )
            stats["inserted"] += conn.total_changes - before

        # Advance the watermark using ALL fills returned, not just BTC ones -
        # otherwise a wallet that trades other coins would refetch the same
        # window forever.
        if fills:
            max_time = max(int(f.get("time") or 0) for f in fills)
            if max_time > (last_fill_time or 0):
                conn.execute(
                    "UPDATE traders SET last_fill_time = ? WHERE address = ?",
                    (max_time, address),
                )
        elif not last_fill_time:
            # No history at all yet: still move the watermark forward so the
            # first-run lookback doesn't restart from scratch every cycle.
            conn.execute(
                "UPDATE traders SET last_fill_time = ? WHERE address = ?",
                (start_time, address),
            )

        stats["polled"] += 1
        conn.commit()
        time.sleep(FILL_CALL_DELAY)

    conn.commit()
    log(
        f"fills: {stats['polled']}/{len(traders)} polled, "
        f"{stats['fetched']} fills seen, {stats['btc_rows']} {COIN} rows, "
        f"{stats['inserted']} new, {stats['errors']} errors"
    )
    return stats


# --------------------------------------------------------------------------
# JSON export (stands in for a live API; the frontend can't tell the difference)
# --------------------------------------------------------------------------

def export_json(conn, web_dir: str = WEB_DIR, hours: int = DEFAULT_HOURS) -> None:
    positions = queries.open_positions(conn)
    hourly = queries.hourly_aggregates(conn, hours=hours)

    for name, payload in (("positions.json", positions), ("hourly.json", hourly)):
        path = os.path.join(web_dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)  # atomic, so the page never reads a half-written file

    s = positions["summary"]
    log(
        f"export: {s['wallets_with_position']} open ({s['long_wallets']}L/"
        f"{s['short_wallets']}S), long ${s['total_long_notional_usd']:,.0f} / "
        f"short ${s['total_short_notional_usd']:,.0f}, "
        f"{len(hourly['buckets'])} hourly buckets"
    )


# --------------------------------------------------------------------------

def cycle(conn) -> None:
    started = time.time()
    poll_positions(conn)
    poll_fills(conn)
    export_json(conn)
    log(f"cycle done in {time.time() - started:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser(description="Hyperliquid BTC tracker poller")
    ap.add_argument("--once", action="store_true", help="run one full cycle and exit")
    ap.add_argument("--positions", action="store_true", help="position poller only")
    ap.add_argument("--fills", action="store_true", help="fill poller only")
    ap.add_argument("--export", action="store_true", help="re-dump JSON only")
    ap.add_argument(
        "--backfill", type=int, metavar="HOURS",
        help="seed history: fetch the last N hours of fills for every wallet",
    )
    args = ap.parse_args()

    conn = db.init()

    n_active = conn.execute(
        "SELECT COUNT(*) AS n FROM traders WHERE is_active = 1"
    ).fetchone()["n"]
    if n_active == 0:
        print("No active traders. Run `python cohort.py` first.", file=sys.stderr)
        sys.exit(1)

    if args.backfill:
        log(f"backfilling {args.backfill}h of fills for {n_active} wallets...")
        poll_fills(conn, lookback_ms=args.backfill * 3600_000)
        export_json(conn); return
    if args.positions:
        poll_positions(conn); export_json(conn); return
    if args.fills:
        poll_fills(conn); export_json(conn); return
    if args.export:
        export_json(conn); return
    if args.once:
        cycle(conn); return

    log(f"tracking {n_active} wallets, cycle every {POLL_INTERVAL_SECONDS}s (ctrl-c to stop)")
    while True:
        try:
            cycle(conn)
        except KeyboardInterrupt:
            log("stopped")
            break
        except Exception as e:  # a bad cycle shouldn't kill the loop
            log(f"cycle failed: {type(e).__name__}: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
