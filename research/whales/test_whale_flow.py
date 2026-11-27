import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whale_flow as w
import score_whales as s


def fill(t, side, sz, start, coin="BTC", px=100.0, crossed=True):
    return {"coin": coin, "px": str(px), "sz": str(sz), "side": side, "time": int(t * 1000),
            "startPosition": str(start), "crossed": crossed}


class TestCandleRule(unittest.TestCase):
    """CLAUDE.md trap 4: a candle key is the bucket START."""
    keys = [0, 60, 120, 180, 240]
    opens = [100.0, 101.0, 102.0, 103.0, 104.0]

    def test_entry_is_the_first_bucket_starting_at_or_after(self):
        self.assertEqual(w.open_at_or_after(self.keys, self.opens, 61), (120, 102.0))
        self.assertEqual(w.open_at_or_after(self.keys, self.opens, 60), (60, 101.0))

    def test_a_fill_inside_a_bucket_never_reads_that_bucket(self):
        # fill at t=30: bucket 0 opened BEFORE it, so its open is not a price a copier can get
        k, p = w.open_at_or_after(self.keys, self.opens, 30)
        self.assertGreaterEqual(k, 30)

    def test_gap_returns_none(self):
        self.assertEqual(w.open_at_or_after([0, 2000], [1.0, 2.0], 100), (None, None))

    def test_planted_lookahead_fails(self):
        """Price jumps inside the fill's own bucket. A leaky rule (bucket containing the
        fill) would credit the jump to the copier; the real rule must not."""
        keys = [0, 60, 120]
        opens = [100.0, 110.0, 110.0]   # the move happened between 0 and 60
        res, _ = w.outcome(keys, opens, 30, +1, delays=(0,), horizons=(60,))
        self.assertAlmostEqual(res[(0, 60)], 0.0)
        leaky = opens[1] / opens[0] - 1
        self.assertNotAlmostEqual(res[(0, 60)], leaky)

    def test_signed_return_and_delay(self):
        res, _ = w.outcome(self.keys, self.opens, 0, -1, delays=(0, 60), horizons=(60,))
        self.assertAlmostEqual(res[(0, 60)], -(101 / 100 - 1))
        self.assertAlmostEqual(res[(60, 60)], -(102 / 101 - 1))


class TestEvents(unittest.TestCase):
    def test_merge_and_kinds(self):
        fs = [fill(0, "B", 1, 0), fill(100, "B", 1, 1), fill(1000, "A", 3, 2), fill(5000, "A", 1, -1),
              fill(9000, "B", 1, -2), fill(9100, "B", 1, -1)]
        ev = w.events(fs)
        self.assertEqual([e["kind"] for e in ev], ["open", "flip", "add", "close"])
        self.assertEqual(ev[0]["n"], 2)
        self.assertEqual(ev[0]["notional"], 200.0)
        self.assertEqual([e["dir"] for e in ev], [1, -1, -1, 1])

    def test_far_apart_fills_are_separate(self):
        ev = w.events([fill(0, "B", 1, 0), fill(1000, "B", 1, 1)])
        self.assertEqual([e["kind"] for e in ev], ["open", "add"])

    def test_coins_are_separate(self):
        ev = w.events([fill(0, "B", 1, 0), fill(10, "B", 1, 0, coin="ETH")])
        self.assertEqual(len(ev), 2)

    def test_taker_share(self):
        ev = w.events([fill(0, "B", 1, 0, crossed=True), fill(10, "B", 1, 1, crossed=False)])
        self.assertAlmostEqual(ev[0]["taker_share"], 0.5)


class TestUniverse(unittest.TestCase):
    def test_ranked_by_account_value_never_pnl(self):
        rows = [{"ethAddress": "0xA", "accountValue": "300000", "windowPerformances": [["allTime", {"pnl": "9e9"}]]},
                {"ethAddress": "0xB", "accountValue": "900000", "windowPerformances": [["allTime", {"pnl": "-1"}]]},
                {"ethAddress": "0xC", "accountValue": "1000", "windowPerformances": [["allTime", {"pnl": "9e9"}]]}]
        self.assertEqual([a for a, _ in w.pick_universe(rows)], ["0xb", "0xa"])


class TestReadOnly(unittest.TestCase):
    def test_guard(self):
        w.assert_read_only()
        for bad in ("order", "cancel", "updateLeverage"):
            with self.assertRaises(ValueError):
                w.info({"type": bad})
        with self.assertRaises(ValueError):
            w.get("https://api.hyperliquid.xyz/exchange")
        with self.assertRaises(ValueError):
            w.get("https://api.exchange.coinbase.com/orders")


class TestScoring(unittest.TestCase):
    def test_bh(self):
        self.assertEqual(s.bh([0.001, 0.5, 0.9]), {0})
        self.assertEqual(s.bh([0.5, 0.6]), set())

    def test_cost_is_subtracted_and_days_cluster(self):
        rows = [{"day": f"d{i%5}", "addr": "a", "k": 0.002} for i in range(50)]
        c = s.cell(rows, "k", 0.001)
        self.assertAlmostEqual(c["mean"], 0.001)
        self.assertEqual(c["days"], 5)
        self.assertEqual(c["win"], 1.0)

    def test_copy_list_uses_only_its_input(self):
        tr = [{"addr": "good", "kind": "open", "k": 0.01 + 0.001 * (i % 3)} for i in range(20)] + \
             [{"addr": "bad", "kind": "open", "k": -0.01} for i in range(20)]
        self.assertEqual(s.copy_list(tr, "k", 0.0), {"good"})


if __name__ == "__main__":
    unittest.main()
