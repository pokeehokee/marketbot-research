import os, sys, unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gas_lead as G  # noqa: E402


class GasLeadTest(unittest.TestCase):
    def test_target_date_both_ticker_shapes(self):
        self.assertEqual(G.target_date("KXAAAGASD-26NOV24"), date(2026, 11, 24))
        self.assertEqual(G.target_date("AAAGASW-23OCT09"), date(2023, 10, 9))
        self.assertEqual(G.target_date("KXAAAGASW-26MAR16TH"), date(2026, 3, 16))

    def test_an_aaa_figure_is_unknown_before_noon_et(self):
        d = date(2026, 11, 20)
        aaa = {date(2026, 11, x): 3.0 + x / 100 for x in range(5, 21)}
        days = sorted(aaa)
        wd = [date(2026, 10, 1) + G.timedelta(days=i) for i in range(45)]
        ws = {x: 2.0 for x in wd}
        before = datetime(2026, 11, 20, 11, 59, tzinfo=G.ET).timestamp()
        after = datetime(2026, 11, 20, 12, 1, tzinfo=G.ET).timestamp()
        tgt = date(2026, 11, 22)
        self.assertEqual(G.features(before, tgt, days, aaa, wd, ws)[0], 3)   # last known is the 19th
        self.assertEqual(G.features(after, tgt, days, aaa, wd, ws)[0], 2)    # the 20th

    def test_a_trade_whose_answer_may_be_out_is_not_used(self):
        aaa = {date(2026, 11, x): 3.0 for x in range(5, 21)}
        wd = [date(2026, 10, 1) + G.timedelta(days=i) for i in range(45)]
        ts = datetime(2026, 11, 20, 13, tzinfo=G.ET).timestamp()
        self.assertIsNone(G.features(ts, date(2026, 11, 20), sorted(aaa), aaa, wd, {x: 2.0 for x in wd}))

    def test_wholesale_is_lagged(self):
        aaa = {date(2026, 11, x): 3.0 for x in range(5, 21)}
        wd = [date(2026, 10, 1) + G.timedelta(days=i) for i in range(52)]   # to Nov 21
        ws = {x: (9.0 if x > date(2026, 11, 13) else 2.0) for x in wd}      # spike only in the last week
        ts = datetime(2026, 11, 20, 13, tzinfo=G.ET).timestamp()
        h, a, m, g = G.features(ts, date(2026, 11, 22), sorted(aaa), aaa, wd, ws)
        self.assertAlmostEqual(g, 2.0 - 3.0)   # the spike is not known yet

    def test_ols_recovers_a_plane(self):
        X = [[1, x, x * x % 7] for x in range(30)]
        y = [0.5 * a + 2 * b - c for a, b, c in X]
        for got, want in zip(G.ols(X, y), (0.5, 2, -1)):
            self.assertAlmostEqual(got, want, places=6)

    def test_source_check_counts_disagreements(self):
        mk = [{"result": "yes", "event_ticker": "KXAAAGASD-26NOV24", "ticker": "a",
               "expiration_value": "3.61", "floor_strike": "3.60"},
              {"result": "yes", "event_ticker": "KXAAAGASD-26NOV24", "ticker": "b",
               "expiration_value": "3.61", "floor_strike": "3.62"}]
        aaa, agree = G.aaa_series(mk, lambda s: None)
        self.assertEqual(agree, 0.5)
        self.assertEqual(aaa, {date(2026, 11, 24): 3.61})


if __name__ == "__main__":
    unittest.main()
