"""Freezes the two new measurements: the chase reference uses only trades of the
SAME side strictly 5-15 minutes earlier (no lookahead, no bid-ask bounce), and
listing age is measured from the market's own open_time."""
import gzip, os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import age_move_screen as A  # noqa: E402
import score_age_move as SA  # noqa: E402


class Chase(unittest.TestCase):
    def test_reference_window_and_side(self):
        t = 10_000
        path = [(t - 600, "yes", 0.40, 10),   # inside, same side
                (t - 600, "no", 0.70, 50),    # inside, other side: ignored
                (t - 100, "yes", 0.90, 99),   # too recent: ignored
                (t + 60, "yes", 0.95, 99),    # after: ignored (no lookahead)
                (t - 2000, "yes", 0.10, 99)]  # too old: ignored
        self.assertAlmostEqual(A.chase_of(path, t, "yes"), 0.40)
        self.assertIsNone(A.chase_of([], t, "yes"))

    def test_future_trades_never_set_the_reference(self):
        t = 10_000
        self.assertIsNone(A.chase_of([(t + 400, "yes", 0.9, 5)], t, "yes"))


class Aggregate(unittest.TestCase):
    def test_age_and_chase_cells(self):
        fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        c = A.db(path)
        open_ts, close_ts = 1_000_000, 1_100_000
        markets = {"S-E-T": ("S-E", "S", open_ts, close_ts, "no")}
        t0 = open_ts + 300                      # 5 minutes after listing
        comp = {("S-E-T", "yes", 0.30, (t0 - 5) // 10): (10, 1),
                ("S-E-T", "yes", 0.45, (t0 + 600 - 5) // 10): (4, 1)}
        A.aggregate_hour(c, 0, comp, 1, 2, markets)
        rows = c.execute("SELECT kind, bucket, band, contracts, paid, taker_won FROM cell "
                         "ORDER BY kind, bucket").fetchall()
        kinds = {(r[0], r[1]) for r in rows}
        self.assertIn(("age", "<10m"), kinds)
        self.assertIn(("age", "10-60m"), kinds)
        # the second yes trade paid 0.45 against 0.30 ten minutes earlier: +15c chase
        self.assertIn(("move", "chase>=10"), kinds)
        mv = [r for r in rows if r[0] == "move"][0]
        self.assertAlmostEqual(mv[4], 4 * 0.45); self.assertEqual(mv[5], 0.0)


class TakerFee(unittest.TestCase):
    def test_taker_fee_at_a_10_lot(self):
        # 0.07*10*0.43*0.57 = 0.1716 -> 18c per 10 -> 1.8c per contract
        self.assertAlmostEqual(SA.taker_fee_per_ct("36-50"), 0.018)


if __name__ == "__main__":
    unittest.main()
