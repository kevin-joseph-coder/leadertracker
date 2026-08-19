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
    POLL_INTERVAL_SECONDS,
    POSITION_CALL_DELAY,
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
    stats = {"polled": 0, "with_position": 0, "deactivated": 0,
             "held_offbook": 0, "errors": 0}
    now_ms = int(time.time() * 1000)

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

        # Drop below the floor -> deactivate, but keep the row and its history
        # so the wallet can be picked back up on the next cohort refresh.
        #
        # marginSummary.accountValue only covers the DEFAULT perp clearinghouse,
        # while the cohort's $100k floor comes from the leaderboard's TOTAL
        # equity (spot + every perp dex). Comparing them directly retired 35 of
        # 100 wallets on the first poll - e.g. one wallet reported perp 0.0
        # while holding $402k of spot USDC. So a wallet that looks under the
        # floor gets one confirming `portfolio` call, which reports the same
        # total the leaderboard does. Healthy wallets cost no extra call.
        if account_value < MIN_ACCOUNT_VALUE:
            total = hl_total_account_value(address)
            if total is None or total < MIN_ACCOUNT_VALUE:
                conn.execute(
                    "UPDATE traders SET is_active = 0, removed_at = ? WHERE address = ?",
                    (now_ms, address),
                )
                stats["deactivated"] += 1
            else:
                stats["held_offbook"] += 1
                conn.execute(
                    "UPDATE traders SET account_value = ? WHERE address = ?",
                    (total, address),
                )
            time.sleep(POSITION_CALL_DELAY)
        else:
            conn.execute(
                "UPDATE traders SET account_value = ? WHERE address = ?",
                (account_value, address),
            )

        stats["polled"] += 1
        conn.commit()
        time.sleep(POSITION_CALL_DELAY)

    conn.commit()
    log(
        f"positions: {stats['polled']}/{len(traders)} polled, "
        f"{stats['with_position']} hold {COIN}, "
        f"{stats['deactivated']} deactivated, "
        f"{stats['held_offbook']} kept (equity outside default perp), "
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
            fills = hl.user_fills_by_time(address, start_time)
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
