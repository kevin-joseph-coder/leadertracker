"""Turn raw Hyperliquid fills into normalized rows for the `fills` table.

Everything here is grounded in real payloads pulled from the live API on
2026-08-19 (100 cohort wallets, 7-day window). See the "What the API
actually returns" section of README.md for the full evidence.

Two findings drive this module:

1. FILL IDENTITY. `hash` is NOT unique per fill - 23,016 fills shared only
   10,273 hashes, because one transaction hash covers every fill it
   produced. `tid` is unique per fill, but it identifies the *match*, so
   both counterparties report the SAME tid: 12 tids showed up on two
   different cohort wallets. Keying on tid alone would silently drop one
   side of any trade between two tracked wallets. So the key is
   f"{address}:{tid}". Across 13,607 real BTC fills that produced 13,607
   distinct keys with zero collisions.

   The one exception: "Spot Dust Conversion" rows carry tid=0 and a
   zero hash, and repeat within a wallet. They are spot-only and never
   BTC perp, but the composite fallback below keeps them from colliding
   if the coin filter is ever widened.

2. FLIPS ARE COMPOUND FILLS. A fill that crosses through zero arrives as
   ONE record with dir "Long > Short" or "Short > Long" - not as two
   clean fills. Real example:

     {"coin":"BTC","px":"63397.0","sz":"0.01","side":"A",
      "startPosition":"0.0074","dir":"Long > Short", ...}

   0.0074 of that 0.01 closed the long; the remaining 0.0026 opened a
   short. So flips are split proportionally by |startPosition| into two
   normalized rows, which lets the aggregation query stay a plain
   GROUP BY over the four standard dir values.
"""

# The four buckets the aggregation query sums over.
OPEN_LONG = "Open Long"
OPEN_SHORT = "Open Short"
CLOSE_LONG = "Close Long"
CLOSE_SHORT = "Close Short"

STANDARD_DIRS = {OPEN_LONG, OPEN_SHORT, CLOSE_LONG, CLOSE_SHORT}

# Observed compound-flip dir values.
FLIP_DIRS = {"Long > Short", "Short > Long"}

# Forced position reductions. These always shrink a position toward zero,
# so they are closes; which side is closing follows startPosition's sign.
# Seen in the wild as "Auto-Deleveraging" (with a `liquidation` key) and
# the "Liquidated ..." family.
REDUCE_ONLY_DIRS = {"Auto-Deleveraging"}
REDUCE_ONLY_PREFIXES = ("Liquidated",)


def to_float(value, default=0.0) -> float:
    """Hyperliquid sends numbers as strings; be forgiving about junk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fill_id(address: str, fill: dict) -> str:
    """Stable unique id for a fill. See finding (1) above."""
    tid = fill.get("tid")
    if tid:  # nonzero, non-None
        return f"{address}:{tid}"
    # tid == 0 / missing: fall back to a composite that stays unique.
    return "{}:0:{}:{}:{}".format(
        address, fill.get("oid", ""), fill.get("time", ""), fill.get("coin", "")
    )


def _row(base_id, suffix, address, fill, direction, sz, closed_pnl):
    px = to_float(fill.get("px"))
    return {
        "fill_id": base_id + suffix,
        "address": address,
        "coin": fill.get("coin"),
        "dir": direction,
        "px": px,
        "sz": sz,
        "notional_usd": px * sz,
        "fill_time": int(fill.get("time") or 0),
        "closed_pnl": closed_pnl,
    }


def _is_reduce_only(direction: str) -> bool:
    return direction in REDUCE_ONLY_DIRS or direction.startswith(REDUCE_ONLY_PREFIXES)


def classify_fill(address: str, fill: dict) -> list:
    """Normalize one raw fill into the rows that should be stored.

    Returns a list because a flip fill becomes two rows (one closing, one
    opening). Ordinary fills return a single row; a degenerate fill with
    size 0 returns nothing.
    """
    direction = (fill.get("dir") or "").strip()
    sz = to_float(fill.get("sz"))
    closed_pnl = to_float(fill.get("closedPnl"))
    base_id = fill_id(address, fill)

    if sz <= 0:
        return []

    if direction in FLIP_DIRS:
        start = abs(to_float(fill.get("startPosition")))
        close_sz = min(sz, start)
        open_sz = sz - close_sz

        if direction == "Long > Short":
            close_dir, open_dir = CLOSE_LONG, OPEN_SHORT
        else:  # "Short > Long"
            close_dir, open_dir = CLOSE_SHORT, OPEN_LONG

        rows = []
        # The realized PnL belongs entirely to the leg that closed.
        if close_sz > 0:
            rows.append(_row(base_id, ":c", address, fill, close_dir, close_sz, closed_pnl))
        if open_sz > 0:
            rows.append(
                _row(base_id, ":o", address, fill, open_dir, open_sz,
                     0.0 if close_sz > 0 else closed_pnl)
            )
        return rows

    if _is_reduce_only(direction):
        start = to_float(fill.get("startPosition"))
        # A forced reduction of a long closes a long, and vice versa.
        close_dir = CLOSE_SHORT if start < 0 else CLOSE_LONG
        return [_row(base_id, "", address, fill, close_dir, sz, closed_pnl)]

    # Standard dirs pass through. Anything else (spot "Buy"/"Sell",
    # "Settlement", "Spot Dust Conversion") is stored with its raw dir and
    # simply falls into none of the four buckets in the aggregation query.
    return [_row(base_id, "", address, fill, direction, sz, closed_pnl)]


def classify_fills(address: str, fills: list, coin: str = None) -> list:
    """Classify a batch, optionally filtering to one coin first."""
    rows = []
    for f in fills:
        if coin is not None and f.get("coin") != coin:
            continue
        rows.extend(classify_fill(address, f))
    return rows
