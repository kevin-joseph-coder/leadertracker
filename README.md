# Hyperliquid BTC cohort tracker (MVP)

Tracks up to 100 Hyperliquid wallets and surfaces their **current open BTC
positions** and **hourly BTC trade flow** on a single static page.

Pure standard library — no pip installs. SQLite for storage, REST polling
only, JSON files as the API layer.

## Quick start

```bash
python cohort.py            # build the 100-wallet cohort -> cohort.json + traders table
python poller.py --backfill 168   # optional: seed 7 days of fill history
python poller.py            # run the pipeline loop (ctrl-c to stop)

# in a second terminal:
python -m http.server 8000  # then open http://localhost:8000
```

The page must be served over HTTP — opening `index.html` as a `file://` URL
fails because `fetch()` can't read the JSON files from that origin.

Refresh cohort membership daily (cron / Task Scheduler):

```bash
python cohort.py
```

## Files

| File | Role |
|---|---|
| `config.py` | All tunables — thresholds, intervals, call delays |
| `db.py` | SQLite schema + connection (`python db.py` initializes) |
| `hl.py` | Hyperliquid REST client (leaderboard, positions, fills) |
| `cohort.py` | Cohort selection → `cohort.json` + `traders` |
| `classify.py` | Fill → bucket classification, including flip splitting |
| `queries.py` | Open-positions and hourly-aggregate reads |
| `poller.py` | Position poller, fill poller, JSON export, main loop |
| `index.html` | The page (Chart.js via CDN, no build step) |
| `test_classify.py` | `python -m unittest test_classify -v` |

`poller.py` flags: `--once`, `--positions`, `--fills`, `--export`,
`--backfill HOURS`.

## What the API actually returns

Everything below was verified against the live API on 2026-08-19, not
assumed. The originally-specified shapes were right in some places and
wrong in others.

### Leaderboard — confirmed as documented

`GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` returns
`{"leaderboardRows": [...]}` exactly as expected: `ethAddress`,
`accountValue`, and `windowPerformances` as `[[window, {pnl, roi, vlm}], ...]`
with windows `day` / `week` / `month` / `allTime`. Rows also carry `prize`
and `displayName`, which we ignore.

It's a **35 MB** response with **42,246 rows**, of which 10,108 sit in the
$100k–$1M band. Fetching it takes a few seconds — fine for a daily job.

### Fill identity — `hash` is NOT unique

Across 23,016 fills there were only **10,273 distinct `hash` values**: one
transaction hash covers every fill it produced. Keying on `hash` would
collapse distinct fills.

`tid` is unique per fill, but it identifies the **match**, so both
counterparties report the same one — **12 tids showed up on two different
cohort wallets**. Keying on `tid` alone would silently drop one side of any
trade between two tracked wallets.

So `fill_id = "{address}:{tid}"`. Across 13,607 real BTC fills that produced
13,607 distinct keys with zero collisions.

The one wrinkle: `"Spot Dust Conversion"` rows carry `tid = 0` and an
all-zero hash, and repeat within a wallet. They're spot-only and never BTC
perp, but `classify.fill_id()` falls back to a composite key for `tid = 0`
so they can't collide if the coin filter is ever widened.

### Flips ARE compound fills

The spec flagged this as unverified. It's now settled: **a position flip
arrives as a single fill** with a non-standard `dir`, not as two clean
records. Real example:

```json
{"coin": "BTC", "px": "63397.0", "sz": "0.01", "side": "A",
 "startPosition": "0.0074", "dir": "Long > Short", "closedPnl": "-6.02656"}
```

0.0074 of that 0.01 closed the long; the remaining 0.0026 opened a short.
`classify.py` splits such a fill into two rows at `|startPosition|` —
`Close Long` + `Open Short` (or `Close Short` + `Open Long`) — so the
aggregation query stays a plain `GROUP BY` over the four standard buckets.
Realized PnL is attributed entirely to the closing leg.

Flips are rare but real: 11 in 66,797 fills (3 of them BTC). `test_classify.py`
uses the actual captured fills as fixtures and asserts the split conserves
both size and notional.

### The full set of `dir` values seen

| `dir` | Handling |
|---|---|
| `Open Long`, `Open Short`, `Close Long`, `Close Short` | The four buckets, stored as-is |
| `Long > Short`, `Short > Long` | Split into a close leg + an open leg |
| `Auto-Deleveraging`, `Liquidated …` | Forced reductions → mapped to a close, side from `startPosition`'s sign |
| `Buy`, `Sell`, `Spot Dust Conversion`, `Settlement` | Spot / non-perp; stored with raw `dir`, land in no bucket |

Only `coin == "BTC"` is stored, which is unambiguously the BTC **perp** —
spot BTC trades under `UBTC`.

### `userFillsByTime` caps at 2000 fills

Confirmed exactly: 4 of the first 14 wallets returned precisely 2000 on a
7-day window. `hl.user_fills_by_time()` pages through the cap, re-requesting
the boundary millisecond (several fills can share one timestamp) and relying
on dedup to absorb the overlap. Without this, busy wallets silently lose
history.

## The $100k floor needs total equity, not perp equity

**This deviates from the spec, deliberately.** The spec says to deactivate a
wallet when `marginSummary.accountValue < 100_000`. Doing exactly that
**retired 35 of 100 wallets on the very first poll** — and they hadn't lost
any money.

`clearinghouseState.marginSummary.accountValue` reports only the **default
perp clearinghouse**. The leaderboard's `accountValue` — the number the
$100k–$1M cohort band is defined against — is **total account equity**: spot,
plus every perp DEX (Hyperliquid now hosts builder-deployed perp DEXs such
as `xyz` for equities). The two are different quantities, so comparing them
against the same threshold is an apples-to-oranges test.

Concretely, `0x0f7ee32…` reports perp `accountValue: 0.0` while holding
$402,229 of spot USDC plus HYPE, UBTC, UETH and USOL. 21 of the 100 cohort
wallets report a perp account value of exactly zero.

The fix: a wallet that looks under the floor gets **one confirming
`portfolio` call**, which reports the same total the leaderboard does, and
is only deactivated if *that* is under $100k. Healthy wallets cost no extra
request, so steady-state polling is unchanged. Result: **35 deactivations → 1**
(that one wallet genuinely fell below the floor).

## Design notes

**Flat snapshots are recorded.** The position poller writes a
`signed_size = 0` row when a wallet holds no BTC. Without it, a wallet that
closed its position would keep showing its last open snapshot forever, since
the open-positions query reads the latest row per wallet.

**The fill watermark advances on all fills, not just BTC ones.** Otherwise a
wallet that trades mostly other coins would re-request the same window every
cycle.

**Overlap is free.** Each poll re-requests the last minute before the
watermark; `INSERT OR IGNORE` on `fill_id` makes re-ingestion idempotent.

**Rate limits.** `/info` is IP-limited by request weight (~1200/min).
`clearinghouseState` costs 2, `userFillsByTime` 20+. With 100 wallets the
fill loop must be spread over more than a minute, hence `FILL_CALL_DELAY =
1.2s` in `config.py`. A full cycle takes roughly 25s + 120s, inside the 300s
interval.

**Cohort ranking is account-wide, not BTC-specific.** Hyperliquid's
leaderboard isn't asset-segmented, so wallets are ranked by overall 7d PnL
and BTC-specificity is applied downstream by filtering to `coin == "BTC"`.
Known simplification, per spec.

## Reading the chart

The four buckets are plotted as a signed stacked bar: **buying pressure
above the axis** (open long, close short), **selling pressure below** (open
short, close long). Position encodes market direction, hue identifies the
bucket. Tooltips show every value as a positive USD notional plus the hourly
net.

**Positioning is state, not flow.** The four bucket columns and Net measure
what the cohort *traded* during an hour; the Positioning column measures what
it *held* at the end of it — total long notional minus total short notional,
across every wallet snapshotted in that hour. It comes from
`position_snapshots` (the last snapshot per wallet inside the hour), not from
`fills`.

It is deliberately not filtered on `is_active`. Snapshots are only ever
written for wallets active at the time, so each hour already contains exactly
the wallets tracked then; filtering on the current flag would retroactively
rewrite past hours whenever a wallet drops out. This is why the newest hour
can differ slightly from the Net exposure tile, which does filter to active
wallets — a wallet deactivated after its last snapshot still counts in that
hour's history.

An hour shows `—` rather than `$0` when no snapshot exists for it. **Position
history only begins when the poller first ran** — `clearinghouseState`
returns current state only, so unlike fills there is nothing to backfill.

**All times are UTC.** The aggregation floors a millisecond epoch, so the
buckets are UTC hour boundaries with no timezone attached; the page renders
every label in UTC to match. Rendering them in browser-local time instead
puts the labels out of step with the data — a whole-hour offset silently
relabels every bucket, and a half-hour offset (IST, ACST) prints `:30`
labels against `:00` data.

Series colors were checked with the data-viz palette validator in both light
and dark mode — worst adjacent CVD ΔE 9.2 light / 9.4 dark, worst
normal-vision ΔE 27.6 / 26.5. Two light-mode series sit below 3:1 contrast
against the surface, so the chart ships a **Table** toggle as the required
relief.

**Expect a long skew.** The cohort is ranked by 7d PnL, so in a rising
market it fills up with wallets that are long. At the time of writing all 33
BTC holders were long and none short — verified against the live API, not a
sign-parsing bug.

## Known limits (deliberate, per the MVP scope)

- Aggregates are computed at read time; no materialized hourly table.
- No WebSockets, no Postgres, no auth, no containers, no alerting.
- `stats-data.hyperliquid.xyz` is unofficial and may change without notice.
- Cohort refresh is a separate manual/cron run, not part of the poll loop.
- A wallet that leaves the cohort keeps its rows (`is_active = 0`), so
  history survives membership churn.
