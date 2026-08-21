"""Tests for the price-level ladder: ATR, mark derivation, entry bucketing.

Run: python -m unittest test_levels -v

price_levels() is exercised against a real in-memory database rather than a
stubbed connection, because half of what it does is read rows through
OPEN_POSITIONS_SQL - including the is_active and latest-snapshot joins, which
a fake row list would quietly skip.
"""

import unittest

from leadertracker import db, queries
from leadertracker.config import ATR_BAND_MULT, LEVEL_BUCKET_PCT


def candle(high, low, close):
    return {"high": high, "low": low, "close": close}


class WilderATRTests(unittest.TestCase):
    def test_needs_period_plus_one_candles(self):
        """N candles give only N-1 true ranges - the first has no prev close."""
        flat = [candle(10, 9, 10)] * 14
        self.assertIsNone(queries.wilder_atr(flat, period=14))
        self.assertIsNotNone(queries.wilder_atr(flat + [candle(10, 9, 10)], period=14))

    def test_empty_and_single(self):
        self.assertIsNone(queries.wilder_atr([], period=14))
        self.assertIsNone(queries.wilder_atr([candle(10, 9, 10)], period=14))

    def test_constant_range_gives_that_range(self):
        """15 identical candles, no gaps: every TR is 1.0, so ATR is 1.0."""
        atr = queries.wilder_atr([candle(101, 100, 100)] * 15, period=14)
        self.assertAlmostEqual(atr, 1.0)

    def test_seed_is_the_mean_of_the_first_period(self):
        """Exactly period+1 candles = exactly period TRs = the plain mean."""
        # TRs alternate 2.0 and 4.0 over 14 ranges -> mean 3.0. Closes are
        # kept inside each candle's range so no gap term ever dominates.
        candles = [candle(100, 100, 100)]
        for i in range(14):
            span = 2.0 if i % 2 == 0 else 4.0
            candles.append(candle(100 + span, 100, 100))
        self.assertAlmostEqual(queries.wilder_atr(candles, period=14), 3.0)

    def test_gap_up_counts_from_the_previous_close(self):
        """The whole point of true range: a gap is range, a bare high-low isn't."""
        # Previous close 100; today trades 120-125. high-low is only 5, but
        # |high - prev_close| is 25, and that is the true range.
        candles = [candle(100, 100, 100)] * 14 + [candle(125, 120, 121)]
        atr = queries.wilder_atr(candles, period=14)
        # Seed over the first 14 TRs is 0 (flat), then one TR of 25 smoothed in.
        self.assertAlmostEqual(atr, 25.0 / 14)
        self.assertGreater(atr, (125 - 120) / 14)  # beats the high-low reading

    def test_smoothing_decays_rather_than_drops(self):
        """A big range keeps influencing ATR after it leaves the seed window."""
        spike = [candle(100, 100, 100)] * 14 + [candle(200, 100, 100)]
        after = spike + [candle(100, 100, 100)] * 5
        self.assertGreater(queries.wilder_atr(after, period=14), 0.0)
        self.assertLess(queries.wilder_atr(after, period=14),
                        queries.wilder_atr(spike, period=14))


class MarkPriceTests(unittest.TestCase):
    def row(self, size, value, poll_time=1000):
        return {"signed_size": size, "position_value_usd": value,
                "poll_time": poll_time}

    def test_derives_mark_from_notional_over_size(self):
        self.assertAlmostEqual(
            queries.mark_price([self.row(460.0, 34236880.0)]), 74428.0)

    def test_shorts_use_absolute_size(self):
        """positionValue is unsigned, so a short must not yield a negative mark."""
        self.assertAlmostEqual(queries.mark_price([self.row(-2.0, 150000.0)]), 75000.0)

    def test_ignores_rows_from_an_older_poll(self):
        """A wallet whose state call failed keeps a stale row at an older mark."""
        rows = [
            self.row(10.0, 750000.0, poll_time=2000),   # 75,000, current
            self.row(10.0, 630000.0, poll_time=1000),   # 63,000, stale
        ]
        self.assertAlmostEqual(queries.mark_price(rows), 75000.0)

    def test_weighted_median_resists_one_outlier(self):
        """Median, not mean: a lone bad row must not drag the estimate."""
        rows = [self.row(10.0, 750000.0), self.row(10.0, 750000.0),
                self.row(1.0, 6300.0)]  # 6,300 - nonsense, small
        self.assertAlmostEqual(queries.mark_price(rows), 75000.0)

    def test_no_usable_rows(self):
        self.assertIsNone(queries.mark_price([]))
        self.assertIsNone(queries.mark_price([self.row(0.0, 0.0)]))


class PriceLevelTests(unittest.TestCase):
    """price_levels() over a real schema, with the ATR pinned by the candles."""

    MARK = 100_000.0
    ATR = 1_000.0  # 15 flat candles of range 1000 -> ATR exactly 1000

    def setUp(self):
        self.conn = db.init(":memory:")
        for i in range(15):
            self.conn.execute(
                "INSERT INTO candles (coin, interval, open_time, high, low, close)"
                " VALUES ('BTC', '1d', ?, ?, ?, ?)",
                (i * 86_400_000, self.MARK + 1000, self.MARK, self.MARK),
            )
        self.conn.commit()
        self.next_addr = 0

    def tearDown(self):
        self.conn.close()

    def add_position(self, signed_size, entry_px, mark=None, poll_time=1000,
                     is_active=1):
        """One active wallet holding one position, priced off `mark`."""
        self.next_addr += 1
        addr = f"0x{self.next_addr:040x}"
        mark = self.MARK if mark is None else mark
        self.conn.execute(
            "INSERT INTO traders (address, is_active) VALUES (?, ?)", (addr, is_active))
        self.conn.execute(
            "INSERT INTO position_snapshots (address, coin, signed_size, entry_px,"
            " unrealized_pnl, position_value_usd, account_value_usd, poll_time)"
            " VALUES (?, 'BTC', ?, ?, 0, ?, 0, ?)",
            (addr, signed_size, entry_px, abs(signed_size) * mark, poll_time))
        self.conn.commit()
        return addr

    def level_at(self, payload, price):
        """The one emitted level whose half-open range contains `price`."""
        hits = [r for r in payload["rows"]
                if r["level_low"] <= price < r["level_high"]]
        self.assertEqual(len(hits), 1, f"no single level covers {price}")
        return hits[0]

    # --- degraded inputs ---

    def test_none_without_candles(self):
        self.conn.execute("DELETE FROM candles")
        self.add_position(1.0, self.MARK)
        self.assertIsNone(queries.price_levels(self.conn))

    def test_none_without_positions(self):
        """No positions means no mark to derive; the candle close stands in,
        but there is then nothing to bucket, so the ladder is still empty."""
        payload = queries.price_levels(self.conn)
        self.assertEqual(payload["mark_source"], "candle")
        self.assertEqual(payload["in_band"]["positions"], 0)

    # --- band geometry ---

    def test_band_is_mark_plus_minus_mult_atr(self):
        self.add_position(1.0, self.MARK)
        p = queries.price_levels(self.conn)
        self.assertAlmostEqual(p["mark_px"], self.MARK)
        self.assertAlmostEqual(p["atr"], self.ATR)
        self.assertAlmostEqual(p["band_low"], self.MARK - ATR_BAND_MULT * self.ATR)
        self.assertAlmostEqual(p["band_high"], self.MARK + ATR_BAND_MULT * self.ATR)

    def test_mark_lands_on_a_level_boundary(self):
        """So the 'current price' marker sits between rows, never mid-bar."""
        self.add_position(1.0, self.MARK)
        p = queries.price_levels(self.conn)
        edges = [r["level_low"] for r in p["rows"]]
        self.assertTrue(any(abs(e - self.MARK) < 1e-6 for e in edges))

    def test_levels_are_contiguous_and_descending(self):
        """Empty levels are emitted too, so the ladder has no silent gaps."""
        self.add_position(1.0, self.MARK)
        rows = queries.price_levels(self.conn)["rows"]
        step = self.MARK * LEVEL_BUCKET_PCT
        for above, below in zip(rows, rows[1:]):
            self.assertGreater(above["level_low"], below["level_low"])
            self.assertAlmostEqual(below["level_high"], above["level_low"])
            self.assertAlmostEqual(above["level_high"] - above["level_low"], step)

    # --- membership ---

    def test_position_exactly_on_the_band_edge_is_included(self):
        self.add_position(1.0, self.MARK + ATR_BAND_MULT * self.ATR)
        self.assertEqual(queries.price_levels(self.conn)["in_band"]["positions"], 1)

    def test_position_past_the_band_edge_is_excluded_but_counted(self):
        self.add_position(1.0, self.MARK + ATR_BAND_MULT * self.ATR + 1)
        p = queries.price_levels(self.conn)
        self.assertEqual(p["in_band"]["positions"], 0)
        self.assertEqual(p["excluded"]["out_of_band"], 1)

    def test_null_entry_px_is_reported_not_dropped(self):
        self.add_position(1.0, None)
        p = queries.price_levels(self.conn)
        self.assertEqual(p["excluded"]["no_entry_px"], 1)
        self.assertEqual(p["in_band"]["positions"], 0)
        self.assertEqual(p["in_band"]["long_notional_usd"], 0.0)

    def test_inactive_wallets_never_reach_the_ladder(self):
        self.add_position(1.0, self.MARK, is_active=0)
        self.assertEqual(queries.price_levels(self.conn)["in_band"]["positions"], 0)

    # --- the numbers ---

    def test_value_is_entry_notional_not_mark_notional(self):
        """5 BTC entered at 99,000 is $495k at the level, whatever the mark is."""
        self.add_position(5.0, 99_000.0)
        lvl = self.level_at(queries.price_levels(self.conn), 99_000.0)
        self.assertAlmostEqual(lvl["long_notional_usd"], 5.0 * 99_000.0)

    def test_longs_and_shorts_are_kept_apart_in_the_same_level(self):
        self.add_position(2.0, 99_100.0)
        self.add_position(-3.0, 99_100.0)
        lvl = self.level_at(queries.price_levels(self.conn), 99_100.0)
        self.assertAlmostEqual(lvl["long_notional_usd"], 2.0 * 99_100.0)
        self.assertAlmostEqual(lvl["short_notional_usd"], 3.0 * 99_100.0)
        self.assertEqual((lvl["long_wallets"], lvl["short_wallets"]), (1, 1))

    def test_level_totals_sum_to_the_in_band_summary(self):
        for px in (98_600.0, 99_400.0, 100_200.0, 101_300.0):
            self.add_position(1.5, px)
            self.add_position(-0.5, px)
        p = queries.price_levels(self.conn)
        self.assertAlmostEqual(
            sum(r["long_notional_usd"] for r in p["rows"]),
            p["in_band"]["long_notional_usd"])
        self.assertAlmostEqual(
            sum(r["short_notional_usd"] for r in p["rows"]),
            p["in_band"]["short_notional_usd"])
        self.assertEqual(p["in_band"]["positions"], 8)

    def test_only_the_latest_snapshot_per_wallet_counts(self):
        """A wallet that moved its entry must not appear at both prices."""
        addr = self.add_position(1.0, 99_000.0, poll_time=1000)
        self.conn.execute(
            "INSERT INTO position_snapshots (address, coin, signed_size, entry_px,"
            " unrealized_pnl, position_value_usd, account_value_usd, poll_time)"
            " VALUES (?, 'BTC', 2.0, 101000.0, 0, 202000.0, 0, 2000)", (addr,))
        self.conn.commit()
        p = queries.price_levels(self.conn)
        self.assertEqual(p["in_band"]["positions"], 1)
        self.assertAlmostEqual(self.level_at(p, 99_000.0)["long_notional_usd"], 0.0)
        self.assertAlmostEqual(
            self.level_at(p, 101_000.0)["long_notional_usd"], 2.0 * 101_000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
