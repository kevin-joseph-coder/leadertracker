"""
Pulls the Hyperliquid leaderboard and selects the cohort in two passes.

Pass 1 (free): shortlist each COHORT_TIERS account-size band on its own
account-wide 7-day metric - dollar PnL for $100k-$1M, ROI for $1M-$5M, so
neither size class crowds out the other.

Pass 2 (one userRole call + two fills calls per candidate): keep only
wallets whose role is in ALLOWED_ROLES and that actually traded BTC in the
window, score them by BTC realized PnL, and take the top TOP_N.

The leaderboard is not asset-segmented, so pass 1 can only rank on
account-wide performance - a wallet can top it without ever touching BTC.
Pass 2 is what makes the cohort BTC-specific: the final ordering comes from
BTC fills alone, and a wallet with no BTC activity is never added.

Run: python -m leadertracker.cohort
Output: cohort.json (the filtered, ranked wallet list) + the `traders`
table in tracker.db. Membership is ADDITIVE - this script never retires a
wallet. Falling out of the ranked top N is not grounds for removal; only
the poller retires, and only on the equity+dormancy test in config.py.

IMPORTANT: stats-data.hyperliquid.xyz is an UNOFFICIAL endpoint that
powers Hyperliquid's own leaderboard webpage. It isn't part of the
documented /info API, so the exact response shape isn't guaranteed
and could change without notice. This script is written defensively
(handles both a bare list and a {"leaderboardRows": [...]} wrapper),
but if it errors out or returns nothing useful, open the Hyperliquid
leaderboard page in a browser, check your Network tab for the actual
request being made, and adjust hl.LEADERBOARD_URL / parse_rows() to
match what you see.

Verified 2026-08-19: the documented shape is what the endpoint actually
returns - {"leaderboardRows": [...]} with ethAddress / accountValue /
windowPerformances[[window, {pnl, roi, vlm}], ...]. Rows also carry
`prize` and `displayName`, which we ignore.

Note: this ranks wallets by overall 7d account performance, not
BTC-specific performance - Hyperliquid's leaderboard isn't asset
segmented. BTC-specificity is applied downstream by filtering fills and
positions to coin == "BTC". Known simplification, not a bug.
"""

import argparse
import json
import time
from dataclasses import dataclass, asdict
from typing import Optional

from . import apilock
from . import db
from . import hl
from .classify import classify_fills
from .config import (
    ALLOWED_ROLES,
    BTC_RANK_LOOKBACK_MS,
    CANDIDATE_POOL,
    COHORT_PATH,
    COHORT_TIERS,
    COIN,
    FILL_CALL_DELAY,
    POSITION_CALL_DELAY,
    RANK_WINDOW,
    TOP_N,
)

# The leaderboard window that supplies the cached 30d volume the poller's
# retirement test reads. Not the same thing as RANK_WINDOW.
VLM_30D_WINDOW = "month"

# How long to wait for the poller to finish its cycle and drop the API lock.
# One cycle is ~11 min at 155 wallets and grows with the panel, so this is
# deliberately generous - the scheduled task allows 2h in total.
POLL_WAIT = 25 * 60


@dataclass
class TraderRow:
    address: str
    account_value: float
    window_pnl: float
    window_roi: float
    window_vlm: float
    vlm_30d: float = 0.0
    # Filled in by pass 2. role is None until the candidate is enriched.
    role: Optional[str] = None
    btc_pnl: float = 0.0
    btc_vlm: float = 0.0
    btc_fills: int = 0


def extract_window(row: dict, window: str) -> Optional[dict]:
    """windowPerformances looks like [[windowName, {pnl, roi, vlm}], ...]."""
    for entry in row.get("windowPerformances", []):
        if len(entry) == 2 and entry[0] == window:
            return entry[1]
    return None


def parse_rows(raw_rows: list, window: str) -> list:
    parsed = []
    for row in raw_rows:
        perf = extract_window(row, window)
        if perf is None:
            continue
        month = extract_window(row, VLM_30D_WINDOW) or {}
        try:
            parsed.append(
                TraderRow(
                    address=row["ethAddress"],
                    account_value=float(row["accountValue"]),
                    window_pnl=float(perf.get("pnl", 0.0)),
                    window_roi=float(perf.get("roi", 0.0)),
                    window_vlm=float(perf.get("vlm", 0.0)),
                    vlm_30d=float(month.get("vlm", 0.0) or 0.0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return parsed


def shortlist_tier(rows: list, tier: dict) -> list:
    """Pass 1 for one size class: [min, max) band, top `pool` by its metric.

    Bounds are half-open so adjacent tiers can't shortlist the same wallet
    twice and pay for its enrichment calls twice over.
    """
    band = [r for r in rows if tier["min"] <= r.account_value < tier["max"]]
    key = ((lambda r: r.window_pnl) if tier["metric"] == "pnl"
           else (lambda r: r.window_roi))
    band.sort(key=key, reverse=True)
    return band[: tier["pool"]]


def shortlist(rows: list, tiers: tuple = COHORT_TIERS) -> list:
    """Pass 1: shortlist each account-size tier on its own metric, concatenated.

    This ordering is only a shortlist - it decides who is worth spending API
    calls on, not who ends up in the cohort. The tiers are ranked on different
    scales (dollars vs percent), so the combined list is deliberately NOT
    globally sorted; build_cohort re-ranks everything on BTC PnL anyway.
    """
    return [r for tier in tiers for r in shortlist_tier(rows, tier)]


def score_btc(address: str, fills: list) -> tuple:
    """(realized BTC pnl, BTC notional, BTC row count) for one wallet.

    Runs the raw fills through the same classifier the poller uses, so flips
    and forced closes are split identically and closed_pnl lands on the leg
    that actually closed.
    """
    rows = classify_fills(address, fills, coin=COIN)
    pnl = sum(r["closed_pnl"] or 0.0 for r in rows)
    vlm = sum(r["notional_usd"] or 0.0 for r in rows)
    return pnl, vlm, len(rows)


def enrich_candidate(
    row: TraderRow,
    lookback_ms: int = BTC_RANK_LOOKBACK_MS,
    now_ms: Optional[int] = None,
) -> TraderRow:
    """Pass 2 for one wallet: resolve its role, then score its BTC trading.

    Mutates and returns the row. Raises hl.HLError so the caller can decide
    whether a failed candidate is fatal (it isn't - main() skips it).
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)

    row.role = hl.user_role(row.address)
    time.sleep(POSITION_CALL_DELAY)

    # No point paying for two paged fills calls on a wallet we'd reject anyway.
    if row.role not in ALLOWED_ROLES:
        return row

    fills = hl.all_fills_by_time(
        row.address, now_ms - lookback_ms, delay=FILL_CALL_DELAY
    )
    row.btc_pnl, row.btc_vlm, row.btc_fills = score_btc(row.address, fills)
    time.sleep(FILL_CALL_DELAY)
    return row


def is_eligible(row: TraderRow) -> bool:
    """A candidate qualifies only if it's a real user account that trades BTC."""
    return row.role in ALLOWED_ROLES and row.btc_fills > 0


def build_cohort(candidates: list, top_n: int = TOP_N) -> list:
    """Final selection: eligible candidates, ranked by BTC realized PnL."""
    eligible = [r for r in candidates if is_eligible(r)]
    eligible.sort(key=lambda r: r.btc_pnl, reverse=True)
    return eligible[:top_n]


def store_cohort(cohort: list, conn=None) -> tuple:
    """Upsert the cohort into `traders`. Purely additive.

    Returns (added, refreshed, reactivated).

    This deliberately does NOT retire wallets that dropped out of the ranked
    top N. The cohort is a tracked panel: once a wallet is in, it stays until
    the poller's equity+dormancy test removes it. Re-ranking every day and
    retiring the losers would churn membership constantly and leave the
    position charts full of wallets with a few hours of history each.

    A wallet the poller previously retired is reactivated if it earns its way
    back in, and keeps its old history and last_fill_time.
    """
    own_conn = conn is None
    conn = conn or db.init()
    now = int(time.time() * 1000)

    existing = {row["address"] for row in conn.execute("SELECT address FROM traders")}
    previously_active = {
        row["address"]
        for row in conn.execute("SELECT address FROM traders WHERE is_active = 1")
    }

    for r in cohort:
        if r.address in existing:
            conn.execute(
                """UPDATE traders
                      SET account_value = ?, vlm_30d = ?, btc_pnl = ?,
                          is_active = 1, removed_at = NULL
                    WHERE address = ?""",
                (r.account_value, r.vlm_30d, r.btc_pnl, r.address),
            )
        else:
            conn.execute(
                """INSERT INTO traders
                       (address, account_value, vlm_30d, btc_pnl,
                        is_active, added_at, last_fill_time)
                   VALUES (?, ?, ?, ?, 1, ?, NULL)""",
                (r.address, r.account_value, r.vlm_30d, r.btc_pnl, now),
            )

    conn.commit()

    addresses = {r.address for r in cohort}
    added = len(addresses - existing)
    reactivated = len((addresses & existing) - previously_active)

    if own_conn:
        conn.close()
    return added, len(addresses) - added - reactivated, reactivated


def sync_vlm_30d(raw_rows: list, conn=None) -> tuple:
    """Refresh traders.vlm_30d for EVERY stored wallet from one leaderboard pull.

    The poller retires on dormancy, so a NULL vlm_30d is a blind spot: the
    wallet can never be judged. Wallets added before the column existed, or
    added by an older cohort.py, have no value until their next selection -
    which may never come, since store_cohort only touches wallets that make
    the top N. This aligns all of them for the cost of one fetch.

    Returns (updated, missing) - missing are wallets no longer on the
    leaderboard at all, which keep NULL rather than being assumed dormant.
    """
    own_conn = conn is None
    conn = conn or db.init()

    vlm = {}
    for row in raw_rows:
        addr = (row.get("ethAddress") or "").lower()
        month = extract_window(row, VLM_30D_WINDOW) or {}
        if addr:
            try:
                vlm[addr] = float(month.get("vlm", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue

    stored = [r["address"] for r in conn.execute("SELECT address FROM traders")]
    updates = [(vlm[a.lower()], a) for a in stored if a.lower() in vlm]
    missing = [a for a in stored if a.lower() not in vlm]

    conn.executemany("UPDATE traders SET vlm_30d = ? WHERE address = ?", updates)
    conn.commit()
    if own_conn:
        conn.close()
    return len(updates), len(missing)


def main():
    raw = hl.fetch_leaderboard()
    parsed = parse_rows(raw, RANK_WINDOW)
    candidates = shortlist(parsed)

    print(f"Fetched {len(raw)} leaderboard rows")
    print(f"{len(parsed)} rows had a '{RANK_WINDOW}' window entry")
    for tier in COHORT_TIERS:
        picked = len(shortlist_tier(parsed, tier))
        in_band = sum(1 for r in parsed
                      if tier["min"] <= r.account_value < tier["max"])
        print(
            f"  ${tier['min']:,.0f}-${tier['max']:,.0f}: {picked} of {in_band} "
            f"in band, top {tier['pool']} by {tier['metric']}"
        )
    print(f"{len(candidates)} candidates shortlisted in total")
    print(f"Enriching (role + {COIN} fills). This takes a while...")

    now_ms = int(time.time() * 1000)
    stats = {"wrong_role": 0, "no_btc": 0, "errors": 0}
    enriched = []

    for i, row in enumerate(candidates, 1):
        try:
            enrich_candidate(row, now_ms=now_ms)
        except hl.HLError as e:
            stats["errors"] += 1
            print(f"  [{i}/{len(candidates)}] {row.address[:10]}... failed: {e}")
            continue

        if row.role not in ALLOWED_ROLES:
            stats["wrong_role"] += 1
        elif row.btc_fills == 0:
            stats["no_btc"] += 1
        else:
            enriched.append(row)

        if i % 25 == 0 or i == len(candidates):
            print(f"  [{i}/{len(candidates)}] {len(enriched)} eligible so far")

    print(
        f"Rejected: {stats['wrong_role']} not in {ALLOWED_ROLES}, "
        f"{stats['no_btc']} with no {COIN} fills, {stats['errors']} errors"
    )

    cohort = build_cohort(enriched)
    print(f"{len(cohort)} wallets selected, ranked by {COIN} realized PnL")
    if len(cohort) < TOP_N:
        print(
            f"  NOTE: fewer than TOP_N={TOP_N}. Only {len(enriched)} of "
            f"{len(candidates)} candidates qualified - raise the tier "
            f"`pool` values in config.py for a deeper pool."
        )

    out_path = COHORT_PATH
    with open(out_path, "w") as f:
        json.dump([asdict(r) for r in cohort], f, indent=2)
    print(f"Wrote cohort to {out_path}")

    added, refreshed, reactivated = store_cohort(cohort)
    print(
        f"traders table: {added} added, {refreshed} refreshed, "
        f"{reactivated} reactivated (none retired - see store_cohort)"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sync-vlm",
        action="store_true",
        help="Only refresh traders.vlm_30d for every stored wallet from the "
             "leaderboard, then exit. One fetch, no per-wallet calls, no "
             "membership changes.",
    )
    args = parser.parse_args()

    # Wait out a poll cycle rather than forfeit the day's refresh: this is a
    # once-daily job, and the poller holds the lock for ~11 minutes at a time.
    # POLL_WAIT covers a full cycle plus slack.
    try:
        with apilock.held("cohort", wait=POLL_WAIT):
            if args.sync_vlm:
                updated, missing = sync_vlm_30d(hl.fetch_leaderboard())
                print(f"vlm_30d refreshed for {updated} wallets")
                if missing:
                    print(f"{missing} not found on the leaderboard - left NULL "
                          f"(unknown, so they will not be retired for dormancy)")
            else:
                main()
    except apilock.Busy as holder:
        print(f"API lock held by {holder} for over {POLL_WAIT / 60:.0f} min - "
              f"giving up rather than competing for the rate limit.",
              file=sys.stderr)
        sys.exit(1)
