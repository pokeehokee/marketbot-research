import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_last_minute as B
import score_btc_last_minute as S


def tape(points):
    t = B.Tape("XBTUSD", lambda s: None, pace=0)
    t.ts = [p[0] for p in points]
    t.px = [p[1] for p in points]
    return t


class TestKrakenParse(unittest.TestCase):
    def test_rows_and_cursor(self):
        rows, last = B.parse_trades({"XXBTZUSD": [["65000.1", "0.01", 1700000000.5, "b", "l", "", 1]],
                                     "last": "1700000000500000000"})
        self.assertEqual(rows, [(1700000000.5, 65000.1, 1)])
        self.assertEqual(last, "1700000000500000000")

    def test_only_trades_path_allowed(self):
        with self.assertRaises(RuntimeError):
            B.kraken_get("/0/private/AddOrder", {})
        with self.assertRaises(RuntimeError):
            B.kraken_get("/0/public/Depth", {})


class TestNoLookahead(unittest.TestCase):
    def setUp(self):
        # one trade per second, price = 100 + second offset, t1 = 1000
        self.t = tape([(float(s), 100.0 + s) for s in range(800, 1003)])

    def test_grid_is_the_last_60_seconds(self):
        g = B.grid(1000)
        self.assertEqual((g[0], g[-1], len(g)), (941, 1000, 60))

    def test_locked_part_uses_only_seconds_known_after_the_lag(self):
        n, locked, fut = B.lock_state(self.t, 1000, 960.5)   # known: grid seconds <= 959.5
        self.assertEqual(n, 19)                               # 941..959
        self.assertAlmostEqual(locked, sum(100.0 + s for s in range(941, 960)))
        self.assertAlmostEqual(fut, sum(100.0 + s for s in range(960, 1001)) / 41)

    def test_before_the_minute_nothing_is_locked(self):
        n, locked, _ = B.lock_state(self.t, 1000, 900.0)
        self.assertEqual((n, locked), (0, 0))

    def test_at_last_trade_at_or_before(self):
        self.assertEqual(self.t.at(950.9), 100.0 + 950)
        self.assertIsNone(self.t.at(799.0))

    def test_stale_price_is_none(self):
        t = tape([(100.0, 1.0)])
        self.assertIsNone(t.at(200.0))


class TestImplied(unittest.TestCase):
    def test_strike_types(self):
        self.assertEqual(B.implied("greater", 10, None, 10.01), "yes")
        self.assertEqual(B.implied("greater", 10, None, 10.0), "no")
        self.assertEqual(B.implied("greater_or_equal", 10, None, 10.0), "yes")
        self.assertEqual(B.implied("less", None, 10, 10.0), "no")
        self.assertIsNone(B.implied("weird", 10, None, 11))


class TestScorer(unittest.TestCase):
    def row(self, **kw):
        r = dict(series="X", sb=0, st="greater", floor=100.0, cap=None, n_locked=59, m=1,
                 locked=59 * 110.0, spot=110.0, rv=0.0002, left=1.0)
        r.update(kw)
        return r

    def test_fixed_part_alone_decides_yes(self):
        # 59 locked seconds at 110 against a strike of 100: the last second cannot pull it under
        p = S.p_yes(self.row(), {("X", 0): [-1.0, 0.0, 1.0] * 20}, 0.0)
        self.assertGreater(p, 0.95)

    def test_basis_shifts_the_strike(self):
        # proxy reads $20 BELOW the settlement index, so a proxy average of 95 is a real 115
        r = self.row(locked=59 * 95.0, spot=95.0)
        no_basis = S.p_yes(r, {("X", 0): [-1.0, 0.0, 1.0] * 20}, 0.0)
        with_basis = S.p_yes(r, {("X", 0): [-1.0, 0.0, 1.0] * 20}, 20.0)
        self.assertLess(no_basis, 0.05)
        self.assertGreater(with_basis, 0.95)

    def test_scorer_finds_and_declines(self):
        rs = [r for r in S.synthetic("random") if r.get("p") is not None]
        self.assertTrue(rs)
        rs = [r for r in S.synthetic("truth") if r.get("p") is not None]
        taken = [r for r in rs if (r["p"] if r["side"] == "yes" else 1 - r["p"]) - r["paid"]
                 - S.W.fee_per_ct(r["paid"], r["n"]) >= 0.05]
        self.assertEqual(taken, [])


class TestBasisMixture(unittest.TestCase):
    def test_deciles(self):
        self.assertEqual(S.deciles(list(range(100))), [5, 15, 25, 35, 45, 55, 65, 75, 85, 95])
        self.assertEqual(S.deciles([]), [0.0])

    def test_spread_basis_is_less_confident_near_the_strike(self):
        # proxy average $3 above the strike: a point basis of 0 says yes with near certainty,
        # a basis that runs -8..+8 says it is close to a coin flip
        r = dict(series="X", sb=0, st="greater", floor=100.0, cap=None, n_locked=59, m=1,
                 locked=59 * 103.0, spot=103.0, rv=0.0002, left=1.0)
        dist = {("X", 0): [-1.0, 0.0, 1.0] * 20}
        point = S.p_yes(r, dist, 0.0)
        spread = S.p_yes(r, dist, [-8.0, -6.0, -4.0, -2.0, 0.0, 0.0, 2.0, 4.0, 6.0, 8.0])
        self.assertGreater(point, 0.95)
        self.assertLess(spread, 0.85)


class TestStreamingMatchesReference(unittest.TestCase):
    """report_stream holds no prints; it must print exactly what the in-memory report prints."""

    def write(self, path):
        import gzip, csv, random
        rng = random.Random(11)
        rows = S.synthetic("random")
        with gzip.open(path, "wt", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(("series", "ticker", "t1", "strike_type", "floor", "cap", "result",
                        "expiration_value", "proxy_avg", "trade_ts", "taker_side", "taker_paid",
                        "count", "n_locked", "locked_sum", "spot", "rv", "fut_avg"))
            for i, r in enumerate(rows):
                side = "no" if i % 3 == 0 else "yes"
                paid = r["paid"] if side == "yes" else round(1 - r["paid"], 2)
                for dup in range(1 + (i % 4 == 0)):          # ties: two prints in one second
                    w.writerow((("SYN", "SYM")[i % 2], r["ticker"], r["t1"], "greater", r["floor"], "",
                                r["result"], r["ev"] + rng.gauss(0, 4), r["pavg"],
                                r["ts"] + 0.25 * dup, side, paid, 1 + (i % 7), r["n_locked"],
                                r["locked"], r["spot"], r["rv"], r["fut"]))

    def run_both(self, path):
        import contextlib, io
        a, b = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(a):
            S.report(S.load(path))
        with contextlib.redirect_stdout(b):
            S.report_stream(path)
        return a.getvalue(), b.getvalue()

    def test_identical_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.csv.gz")
            self.write(p)
            ref, got = self.run_both(p)
        self.assertIn("priced out of sample", ref)
        self.assertIn("EV", ref)
        self.assertEqual(ref, got)


if __name__ == "__main__":
    unittest.main()
