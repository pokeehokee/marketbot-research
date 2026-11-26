import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forecastex_bias as F


def row(c, sd, d, end, q=0, vwap=0.0, exp="2026-11-10T08:00:00-06:00", start=0.0, high=0.0, low=0.0):
    return dict(event_contract=c, subtype=sd, expiration_date=exp, date=d, end_price=f"{end:.2f}",
                pair_quantity=str(q), vwap=f"{vwap:.2f}", start_price=f"{start:.2f}",
                high_price=f"{high:.2f}", low_price=f"{low:.2f}")


class TestBuild(unittest.TestCase):
    def files(self, final_yes=1.0, final_no=0.0, trade_on_expiry=False):
        rs = [row("A_1_5", "YES", "2026-11-08", 0.90, q=10, vwap=0.91),
              row("A_1_5", "NO", "2026-11-08", 0.10, q=10, vwap=0.09),
              row("A_1_5", "YES", "2026-11-09", 0.92),             # no trade: not an entry
              row("A_1_5", "NO", "2026-11-09", 0.08),
              row("A_1_5", "YES", "2026-11-10", 0.95, q=5 if trade_on_expiry else 0, vwap=0.40),
              row("A_1_5", "NO", "2026-11-10", 0.05),
              row("A_1_5", "YES", "2026-11-11", final_yes), row("A_1_5", "NO", "2026-11-11", final_no)]
        return rs

    def test_entry_is_last_traded_day_strictly_before_expiry(self):
        obs, _ = F.build(self.files(trade_on_expiry=True))
        self.assertEqual((obs[0]["entry_day"], obs[0]["p_yes"], obs[0]["days_left"]), ("2026-11-08", 0.91, 2))

    def test_expiry_day_mark_is_not_a_resolution(self):
        # the expiry-day row reads 0.95: skipped, the next day's 1.00 is the outcome
        obs, _ = F.build(self.files(final_yes=0.0, final_no=1.0))
        self.assertEqual(obs[0]["result"], "no")

    def test_unresolved_is_dropped(self):
        fs = self.files()
        fs = [r for r in fs if r["date"] != "2026-11-11"]
        obs, drops = F.build(fs)
        self.assertEqual(obs, [])
        self.assertEqual(drops["no 0/1 row after expiry yet"], 1)

    def test_resolved_contract(self):
        fs = self.files()
        fs = [r for r in fs if r["date"] != "2026-11-10"]
        obs, _ = F.build(fs)
        self.assertEqual(obs[0]["result"], "yes")

    def test_sides_must_agree(self):
        fs = self.files(final_yes=1.0, final_no=1.0)
        fs = [r for r in fs if r["date"] != "2026-11-10"]
        obs, drops = F.build(fs)
        self.assertEqual(obs, [])
        self.assertEqual(drops["YES and NO resolutions disagree"], 1)


    def test_a_no_trade_zero_row_is_skipped_not_a_verdict(self):
        # fx69-1 stopped at the first row with YES at 0 and dropped the contract; a later
        # complementary row is the real resolution
        fs = [r for r in self.files() if r["date"] != "2026-11-10"]
        fs += [row("A_1_5", "YES", "2026-11-10", 0.0), row("A_1_5", "NO", "2026-11-10", 0.0)]
        obs, drops = F.build(fs)
        self.assertEqual(obs[0]["result"], "yes")
        self.assertEqual(drops["YES and NO resolutions disagree"], 0)

    def test_early_entry_is_two_days_before_the_expiry_date(self):
        fs = self.files(trade_on_expiry=False)
        fs.append(row("A_1_5", "YES", "2026-11-09", 0.93, q=3, vwap=0.93))   # the measured day
        obs, _ = F.build(fs)
        self.assertEqual((obs[0]["entry_day"], obs[0]["early_entry_day"]), ("2026-11-09", "2026-11-08"))
        self.assertEqual(obs[0]["early_days_left"], 2)


class TestScore(unittest.TestCase):
    def test_worst_price_is_the_dear_sides_dearest_print(self):
        o = dict(p_yes=0.9, high=0.95, low=0.85, start=0.88, result="yes")
        self.assertEqual(F.dear(o, "", "worst"), (0.95, True))
        self.assertEqual(F.dear(o, "", "start"), (0.88, True))
        o = dict(p_yes=0.1, high=0.2, low=0.04, start=0.12, result="no")
        p, won = F.dear(o, "", "worst")
        self.assertAlmostEqual(p, 0.96); self.assertTrue(won)        # NO's dearest = 1 - YES low
        p, _ = F.dear(o, "", "start")
        self.assertAlmostEqual(p, 0.88)

    def test_missing_price_kind_is_skipped_not_zero(self):
        self.assertIsNone(F.dear(dict(p_yes=0.9, high="", result="yes"), "", "worst"))
        self.assertIsNone(F.dear(dict(early_p_yes="", result="yes"), "early_", "vwap"))

    def test_dear_side(self):
        self.assertEqual(F.dear(dict(p_yes=0.9, result="yes")), (0.9, True))
        p, won = F.dear(dict(p_yes=0.2, result="yes"))
        self.assertAlmostEqual(p, 0.8); self.assertFalse(won)

    def test_fee_is_charged(self):
        F.LOG.clear()
        obs = [dict(p_yes=0.9, result="yes", expiry=f"2026-11-{d:02d}") for d in range(1, 9)]
        F.score(obs, "t", 0.8, 0.97)
        self.assertIn("EV +9.00c/ct", F.LOG[-1])          # 1 - 0.90 - 0.01


class TestReadOnly(unittest.TestCase):
    def test_coinbase_candles_only(self):
        with self.assertRaises(RuntimeError):
            F.P.get("https://api.exchange.coinbase.com/orders")


if __name__ == "__main__":
    unittest.main()
