import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lip_scan as L  # noqa: E402


class LipTest(unittest.TestCase):
    def test_reference_price_is_where_a_fifth_of_target_has_accumulated(self):
        lv = [(0.50, 100), (0.49, 150), (0.48, 1000)]
        ref, s = L.side_score(lv, 1000, 0.5)
        self.assertEqual(ref, 0.49)               # 100 < 200 <= 250
        # 0.50 and 0.49 full credit, 0.48 one tick below at 0.5
        self.assertAlmostEqual(s, 100 + 150 + 500)

    def test_a_side_short_of_target_does_not_qualify(self):
        self.assertIsNone(L.side_score([(0.5, 300), (0.4, 600)], 1000, 0.5))
        self.assertIsNone(L.side_score([], 1000, 0.5))

    def test_small_order_share_and_collateral(self):
        book = {"yes_dollars": [["0.48", "1000"], ["0.50", "990"]],   # ascending, as served
                "no_dollars": [["0.40", "500"]]}                     # NO side short of target
        share, coll, n = L.market_take(book, 1000, 0.5, q=10)
        self.assertEqual(n, 1)
        # 0.48 sits two ticks under the 0.50 reference: 1000 x 0.5^2 = 250
        self.assertAlmostEqual(share, 10 / (990 + 250 + 10))
        self.assertAlmostEqual(coll, 10 * 0.50)

    def test_daily_reward_is_centi_cents(self):
        r = {"period_reward": 3_000_000, "start_date": "2026-11-30T15:00:00Z",
             "end_date": "2026-12-07T15:00:00Z"}
        self.assertAlmostEqual(L.daily_reward(r), 300 / 7)

    def test_the_whitelist_refuses_anything_else(self):
        with self.assertRaises(AssertionError):
            L.get("/portfolio/orders", {})


if __name__ == "__main__":
    unittest.main()
