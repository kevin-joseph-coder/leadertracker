"""
Pulls the Hyperliquid leaderboard, ranks wallets by their 7-day (week)
window performance, and filters to the $100k-$1M account value band.

Run: python cohort.py
Output: cohort.json (the filtered, ranked wallet list) + the `traders`
table in tracker.db, with membership refreshed in place.

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

import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Optional

import db
import hl
from config import (
    BASE_DIR,
    MAX_ACCOUNT_VALUE,
    MIN_ACCOUNT_VALUE,
    RANK_METRIC,
    RANK_WINDOW,
    TOP_N,
)


@dataclass
class TraderRow:
    address: str
    account_value: float
    window_pnl: float
    window_roi: float
    window_vlm: float


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
        try:
            parsed.append(
                TraderRow(
                    address=row["ethAddress"],
                    account_value=float(row["accountValue"]),
                    window_pnl=float(perf.get("pnl", 0.0)),
                    window_roi=float(perf.get("roi", 0.0)),
                    window_vlm=float(perf.get("vlm", 0.0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return parsed


def build_cohort(
    rows: list,
    min_value: float = MIN_ACCOUNT_VALUE,
    max_value: float = MAX_ACCOUNT_VALUE,
    top_n: int = TOP_N,
    metric: str = RANK_METRIC,
) -> list:
    band = [r for r in rows if min_value <= r.account_value <= max_value]
    key = (lambda r: r.window_pnl) if metric == "pnl" else (lambda r: r.window_roi)
    band.sort(key=key, reverse=True)
    return band[:top_n]


def store_cohort(cohort: list, conn=None) -> tuple:
    """Upsert the cohort into `traders`, retiring wallets that dropped out.

    Returns (added, kept, retired). Rows are never deleted - a retired
    wallet keeps its history and its last_fill_time in case it comes back.
    """
    own_conn = conn is None
    conn = conn or db.init()
    now = int(time.time() * 1000)
    addresses = [r.address for r in cohort]

    existing = {row["address"] for row in conn.execute("SELECT address FROM traders")}
    previously_active = {
        row["address"]
        for row in conn.execute("SELECT address FROM traders WHERE is_active = 1")
    }

    for r in cohort:
        if r.address in existing:
            conn.execute(
                """UPDATE traders
                      SET account_value = ?, is_active = 1, removed_at = NULL
                    WHERE address = ?""",
                (r.account_value, r.address),
            )
        else:
            conn.execute(
                """INSERT INTO traders
                       (address, account_value, is_active, added_at, last_fill_time)
                   VALUES (?, ?, 1, ?, NULL)""",
                (r.address, r.account_value, now),
            )

    retired = previously_active - set(addresses)
    if retired:
        conn.executemany(
            "UPDATE traders SET is_active = 0, removed_at = ? WHERE address = ?",
            [(now, a) for a in retired],
        )

    conn.commit()
    if own_conn:
        conn.close()

    added = len(set(addresses) - existing)
    return added, len(addresses) - added, len(retired)


def main():
    raw = hl.fetch_leaderboard()
    parsed = parse_rows(raw, RANK_WINDOW)
    cohort = build_cohort(parsed)

    print(f"Fetched {len(raw)} leaderboard rows")
    print(f"{len(parsed)} rows had a '{RANK_WINDOW}' window entry")
    print(
        f"{len(cohort)} wallets in the ${MIN_ACCOUNT_VALUE:,.0f}-"
        f"${MAX_ACCOUNT_VALUE:,.0f} band, ranked by {RANK_METRIC}"
    )

    out_path = os.path.join(BASE_DIR, "cohort.json")
    with open(out_path, "w") as f:
        json.dump([asdict(r) for r in cohort], f, indent=2)
    print(f"Wrote cohort to {out_path}")

    added, kept, retired = store_cohort(cohort)
    print(f"traders table: {added} added, {kept} kept active, {retired} retired")


if __name__ == "__main__":
    main()
