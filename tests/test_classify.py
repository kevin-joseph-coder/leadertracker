"""Tests for fill classification.

Run: python -m unittest test_classify -v

The flip fixtures are REAL fills pulled from the live API on 2026-08-19,
not invented ones - flips genuinely arrive as a single compound record.
"""

import unittest

from leadertracker.classify import classify_fill, classify_fills, fill_id

# --- real BTC flip fills, verbatim from userFillsByTime ---

REAL_FLIP_LONG_TO_SHORT = {
    "coin": "BTC", "px": "63397.0", "sz": "0.01", "side": "A",
    "time": 1786576466993, "startPosition": "0.0074", "dir": "Long > Short",
    "closedPnl": "-6.02656",
    "hash": "0x4bb204225eab1e5c4d2b044209af8d01d1001c07f9ae3d2eef7aaf751daef846",
    "oid": 515519292353, "crossed": True, "fee": "0.221889",
    "tid": 532588260425135, "feeToken": "USDC", "twapId": None,
}
FLIP_L2S_ADDR = "0x734531e87f8c1643321355f7143fcc6169c4b404"

REAL_FLIP_SHORT_TO_LONG = {
    "coin": "BTC", "px": "63687.0", "sz": "0.15394", "side": "B",
    "time": 1786596239064, "startPosition": "-0.1", "dir": "Short > Long",
    "closedPnl": "-29.0",
    "hash": "0xea483e6560ed0915ebc104420de6f20202dc004afbe027e78e10e9b81fe0e300",
    "oid": 515661668932, "crossed": True, "fee": "3.431391",
    "tid": 661276951639663, "feeToken": "USDC", "twapId": None,
}

REAL_OPEN_LONG = {
    "coin": "BTC", "px": "63589.0", "sz": "0.10244", "side": "B",
    "time": 1786583811115, "startPosition": "28.66593", "dir": "Open Long",
    "closedPnl": "0.0", "hash": "0x4c9aebb7", "oid": 515576612328,
    "crossed": True, "fee": "2.605622", "tid": 578348764744976,
    "feeToken": "USDC", "twapId": None,
}

ADDR = "0xabc0000000000000000000000000000000000001"
OTHER = "0xabc0000000000000000000000000000000000002"


class TestStandardFills(unittest.TestCase):
    def test_open_long_passes_through(self):
        rows = classify_fill(ADDR, REAL_OPEN_LONG)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["dir"], "Open Long")
        self.assertAlmostEqual(r["sz"], 0.10244)
        self.assertAlmostEqual(r["notional_usd"], 63589.0 * 0.10244, places=6)
        self.assertEqual(r["fill_time"], 1786583811115)

    def test_notional_is_px_times_sz(self):
        for d in ("Open Long", "Open Short", "Close Long", "Close Short"):
            f = dict(REAL_OPEN_LONG, dir=d)
            r = classify_fill(ADDR, f)[0]
            self.assertEqual(r["dir"], d)
            self.assertAlmostEqual(r["notional_usd"], r["px"] * r["sz"], places=9)

    def test_zero_size_fill_is_dropped(self):
        self.assertEqual(classify_fill(ADDR, dict(REAL_OPEN_LONG, sz="0.0")), [])


class TestFlipFills(unittest.TestCase):
    """A flip is ONE fill that must become two rows."""

    def test_long_to_short_splits_at_start_position(self):
        rows = classify_fill(FLIP_L2S_ADDR, REAL_FLIP_LONG_TO_SHORT)
        self.assertEqual(len(rows), 2)
        close, open_ = rows

        self.assertEqual(close["dir"], "Close Long")
        self.assertAlmostEqual(close["sz"], 0.0074)
        self.assertAlmostEqual(close["notional_usd"], 63397.0 * 0.0074, places=6)

        self.assertEqual(open_["dir"], "Open Short")
        self.assertAlmostEqual(open_["sz"], 0.0026)
        self.assertAlmostEqual(open_["notional_usd"], 63397.0 * 0.0026, places=6)

    def test_short_to_long_splits_at_start_position(self):
        rows = classify_fill(ADDR, REAL_FLIP_SHORT_TO_LONG)
        self.assertEqual(len(rows), 2)
        close, open_ = rows

        self.assertEqual(close["dir"], "Close Short")
        self.assertAlmostEqual(close["sz"], 0.1)

        self.assertEqual(open_["dir"], "Open Long")
        self.assertAlmostEqual(open_["sz"], 0.05394)

    def test_split_conserves_size_and_notional(self):
        """No size may be invented or lost by the split."""
        for f in (REAL_FLIP_LONG_TO_SHORT, REAL_FLIP_SHORT_TO_LONG):
            rows = classify_fill(ADDR, f)
            self.assertAlmostEqual(sum(r["sz"] for r in rows), float(f["sz"]), places=9)
            self.assertAlmostEqual(
                sum(r["notional_usd"] for r in rows),
                float(f["px"]) * float(f["sz"]),
                places=6,
            )

    def test_realized_pnl_attributed_to_closing_leg_only(self):
        rows = classify_fill(ADDR, REAL_FLIP_LONG_TO_SHORT)
        self.assertAlmostEqual(rows[0]["closed_pnl"], -6.02656)
        self.assertEqual(rows[1]["closed_pnl"], 0.0)

    def test_flip_legs_get_distinct_ids(self):
        rows = classify_fill(ADDR, REAL_FLIP_LONG_TO_SHORT)
        self.assertEqual(len({r["fill_id"] for r in rows}), 2)
        self.assertTrue(all(r["fill_id"].startswith(f"{ADDR}:") for r in rows))

    def test_synthetic_exact_flip_to_flat_emits_only_a_close(self):
        """sz == |startPosition|: closes out, opens nothing."""
        f = dict(REAL_FLIP_LONG_TO_SHORT, sz="0.0074", startPosition="0.0074")
        rows = classify_fill(ADDR, f)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dir"], "Close Long")
        self.assertAlmostEqual(rows[0]["sz"], 0.0074)

    def test_synthetic_flip_never_yields_negative_notional(self):
        """Guards the aggregation's 'non-negative notional' requirement."""
        weird = dict(REAL_FLIP_LONG_TO_SHORT, sz="0.005", startPosition="0.0074")
        for r in classify_fill(ADDR, weird):
            self.assertGreaterEqual(r["sz"], 0.0)
            self.assertGreaterEqual(r["notional_usd"], 0.0)


class TestForcedReductions(unittest.TestCase):
    def test_adl_on_a_long_is_a_close_long(self):
        f = dict(REAL_OPEN_LONG, dir="Auto-Deleveraging", startPosition="2.5")
        self.assertEqual(classify_fill(ADDR, f)[0]["dir"], "Close Long")

    def test_adl_on_a_short_is_a_close_short(self):
        f = dict(REAL_OPEN_LONG, dir="Auto-Deleveraging", startPosition="-2.5")
        self.assertEqual(classify_fill(ADDR, f)[0]["dir"], "Close Short")

    def test_liquidation_of_a_long_is_a_close_long(self):
        f = dict(REAL_OPEN_LONG, dir="Liquidated Cross", startPosition="2.5")
        self.assertEqual(classify_fill(ADDR, f)[0]["dir"], "Close Long")


class TestFillIdentity(unittest.TestCase):
    """tid identifies the MATCH, so it is shared by both counterparties."""

    def test_same_tid_on_two_wallets_yields_two_ids(self):
        a = fill_id(ADDR, REAL_OPEN_LONG)
        b = fill_id(OTHER, REAL_OPEN_LONG)
        self.assertNotEqual(a, b)

    def test_same_fill_same_wallet_is_stable_across_polls(self):
        """Overlapping refetches must produce an identical key."""
        self.assertEqual(fill_id(ADDR, REAL_OPEN_LONG), fill_id(ADDR, dict(REAL_OPEN_LONG)))

    def test_hash_is_not_used_as_the_key(self):
        """Two fills sharing one tx hash must still get distinct ids."""
        f1 = dict(REAL_OPEN_LONG, tid=111)
        f2 = dict(REAL_OPEN_LONG, tid=222)  # same hash, different fill
        self.assertNotEqual(fill_id(ADDR, f1), fill_id(ADDR, f2))

    def test_zero_tid_dust_rows_do_not_collide(self):
        """Real 'Spot Dust Conversion' rows carry tid=0 and a zero hash."""
        d1 = {"coin": "@151", "px": "1877.5", "sz": "0.000076354", "dir": "Spot Dust Conversion",
              "time": 1786579200046, "oid": 515536926718, "tid": 0, "startPosition": "0.000076354"}
        d2 = {"coin": "@151", "px": "1885.0", "sz": "0.000076354", "dir": "Spot Dust Conversion",
              "time": 1786665600060, "oid": 516246665154, "tid": 0, "startPosition": "0.000076354"}
        self.assertNotEqual(fill_id(ADDR, d1), fill_id(ADDR, d2))


class TestBatchFiltering(unittest.TestCase):
    def test_coin_filter_keeps_only_btc(self):
        batch = [
            REAL_OPEN_LONG,
            {"coin": "@708", "px": "39.8", "sz": "236.95", "dir": "Sell",
             "time": 1786561867880, "tid": 465575977390996, "startPosition": "2450.3"},
            REAL_FLIP_LONG_TO_SHORT,
        ]
        rows = classify_fills(ADDR, batch, coin="BTC")
        self.assertEqual(len(rows), 3)  # 1 open + 2 flip legs
        self.assertTrue(all(r["coin"] == "BTC" for r in rows))

    def test_spot_dirs_are_kept_but_land_in_no_bucket(self):
        spot = {"coin": "@708", "px": "39.8", "sz": "10", "dir": "Sell",
                "time": 1786561867880, "tid": 1, "startPosition": "100"}
        rows = classify_fills(ADDR, [spot])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dir"], "Sell")
        self.assertNotIn(rows[0]["dir"], {"Open Long", "Open Short", "Close Long", "Close Short"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
