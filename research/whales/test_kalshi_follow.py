import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kalshi_follow as k


class TestKalshiFollow(unittest.TestCase):
    closes = [(900, "A"), (1800, "B"), (2700, "C")]

    def test_window_open_at_entry_with_time_left(self):
        self.assertEqual(k.market_for(self.closes, 100), (900, "A"))
        self.assertIsNone(k.market_for(self.closes, 800))   # A has < 2 min left, B not open yet
        self.assertEqual(k.market_for(self.closes, 950), (1800, "B"))
        self.assertIsNone(k.market_for(self.closes, 2700))

    def test_entry_never_uses_a_print_before_the_copier_could_act(self):
        tr = [(99, "yes", .40), (101, "no", .55), (105, "yes", .52)]
        self.assertEqual(k.entry(tr, 100, "yes"), (105, .52))
        self.assertEqual(k.entry(tr, 100, "no"), (101, .55))
        self.assertIsNone(k.entry(tr, 100, "yes", wait=3))

    def test_fee_rounds_up_per_order(self):
        self.assertAlmostEqual(k.fee(.5), .018)
        self.assertAlmostEqual(k.fee(.95), .004)

    def test_read_only(self):
        with self.assertRaises(RuntimeError):
            k.T.assert_read_only("/portfolio/orders")


if __name__ == "__main__":
    unittest.main()
