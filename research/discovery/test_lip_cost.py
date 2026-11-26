import os, sqlite3, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lip_cost as C  # noqa: E402


def trade(tk, ts, side, yes_price, count, tid):
    return {"ticker": tk, "created_time": ts, "taker_outcome_side": side,
            "yes_price_dollars": f"{yes_price:.4f}", "count_fp": f"{count:.2f}", "trade_id": tid}


class MakerArithmetic(unittest.TestCase):
    def test_maker_wins_when_taker_side_loses(self):
        # taker buys YES at 0.30, market resolves NO: the maker (who acquired NO at 0.70) keeps 0.30
        tr = [trade("M", "2026-11-01T10:00:00Z", "yes", 0.30, 10, "a")]
        close = C.T.utc("2026-11-01T12:00:00Z")
        ct, pnl, fee, lh_ct, lh_pnl, n = C.aggregate("M", tr, "no", close, "quadratic")
        self.assertEqual(ct, 10)
        self.assertAlmostEqual(pnl, 3.0)
        self.assertEqual(fee, 0.0)

    def test_maker_loses_when_taker_side_wins_and_no_price_is_used(self):
        # taker buys NO when YES trades at 0.80: the taker paid 0.20 for NO; NO wins -> maker -0.80/ct
        tr = [trade("M", "2026-11-01T11:30:00Z", "no", 0.80, 5, "b")]
        close = C.T.utc("2026-11-01T12:00:00Z")
        ct, pnl, fee, lh_ct, lh_pnl, n = C.aggregate("M", tr, "no", close, "quadratic")
        self.assertAlmostEqual(pnl, 5 * (0.20 - 1.0))
        self.assertEqual(lh_ct, 5)   # inside the last hour

    def test_duplicate_trade_ids_and_foreign_tickers_and_late_trades_are_dropped(self):
        close = C.T.utc("2026-11-01T12:00:00Z")
        tr = [trade("M", "2026-11-01T10:00:00Z", "yes", 0.5, 1, "x"),
              trade("M", "2026-11-01T10:00:00Z", "yes", 0.5, 1, "x"),
              trade("OTHER", "2026-11-01T10:00:00Z", "yes", 0.5, 1, "y"),
              trade("M", "2026-11-01T12:05:00Z", "yes", 0.5, 1, "z")]
        self.assertEqual(C.aggregate("M", tr, "yes", close, None)[0], 1)

    def test_maker_fee_only_on_maker_fee_types_and_rounded_up(self):
        self.assertEqual(C.maker_fee("quadratic", 10, 0.5), 0.0)
        # 0.0175 x 10 x .25 = 0.04375 dollars -> 4.375c -> 5c
        self.assertAlmostEqual(C.maker_fee("quadratic_with_maker_fees", 10, 0.5), 0.05)


class Pool(unittest.TestCase):
    def test_pro_rata_over_the_open_part_of_the_period(self):
        day = 86400.0
        # $70 over 7 days; market open days 0-3.5 -> $35
        self.assertAlmostEqual(C.pool_for([(0.0, 7 * day, 70.0)], 0.0, 3.5 * day), 35.0)
        # a programme that ended before the market opened pays nothing
        self.assertEqual(C.pool_for([(0.0, day, 10.0)], 2 * day, 3 * day), 0.0)


class Verdict(unittest.TestCase):
    def rows(self, e_per_day, days=20, losers_per_day=2):
        out = []
        for d in range(days):
            for k in range(losers_per_day):
                out.append(dict(ticker=f"L{d}{k}", series="S", day=f"2026-10-{d + 1:02d}",
                                contracts=10, pnl=-1.0, fee=0.0, pool=0.0, lh_ct=0, lh_pnl=0, f=1.0))
            out.append(dict(ticker=f"W{d}", series="S", day=f"2026-10-{d + 1:02d}", contracts=10,
                            pnl=0.0, fee=0.0, pool=e_per_day + 2.0, lh_ct=0, lh_pnl=0, f=1.0))
        return out

    def test_pass_needs_positive_bounds_and_thirty_losers(self):
        v = C.verdict(self.rows(1.0))
        self.assertTrue(v["pass"])
        self.assertEqual(v["losers"], 40)

    def test_negative_e_fails(self):
        self.assertFalse(C.verdict(self.rows(-1.0))["pass"])

    def test_too_few_losers_fails_however_positive(self):
        self.assertFalse(C.verdict(self.rows(5.0, days=10, losers_per_day=2))["pass"])

    def test_two_sided_fraction_scales_the_pool(self):
        r = dict(pool=10.0, pnl=-6.0, fee=0.0, f=0.5)
        self.assertAlmostEqual(C.e_of(r), -1.0)
        self.assertAlmostEqual(C.e_of(r, use_f=False), 4.0)


class ReadOnly(unittest.TestCase):
    def test_programme_reader_cannot_reach_a_write_path(self):
        with self.assertRaises(AssertionError):
            C.P.get("/portfolio/orders", {})

    def test_series_file_parses_fraction_and_comments(self):
        p = tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt")
        p.write("# head\nKXA 0.5\nKXB\n"); p.close()
        self.assertEqual(C.read_series_file(p.name), {"KXA": 0.5, "KXB": 1.0})


if __name__ == "__main__":
    unittest.main()
