import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_sharp as p


class TestPmSharp(unittest.TestCase):
    def test_winner(self):
        self.assertEqual(p.winner({"outcomes": '["Yes","No"]', "outcomePrices": '["0","1"]'}), 1)
        self.assertIsNone(p.winner({"outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]'}))
        self.assertIsNone(p.winner({"outcomes": '["A","B","C"]', "outcomePrices": '["1","0","0"]'}))

    def test_value(self):
        self.assertAlmostEqual(p.value("BUY", 0.3, 10, 1.0), 7.0)
        self.assertAlmostEqual(p.value("SELL", 0.3, 10, 1.0), -7.0)
        self.assertAlmostEqual(p.value("SELL", 0.3, 10, 0.0), 3.0)

    def test_follow_never_uses_an_earlier_print(self):
        rows = {"M": [("M", "x", "BUY", 0, .40, 10, 100), ("M", "y", "BUY", 0, .45, 5, 150),
                      ("M", "z", "BUY", 0, .55, 5, 200)]}
        out = p.follow(rows, {"x"}, {"M": 0}, {"M": 86400 * 3}, delay=60)
        self.assertEqual(out[3], [(1 - .55, .55, 1.0)])   # the 150 print is inside the delay

    def test_sell_is_followed_by_buying_the_other_side(self):
        rows = {"M": [("M", "x", "SELL", 0, .60, 10, 100), ("M", "y", "BUY", 1, .42, 5, 200)]}
        out = p.follow(rows, {"x"}, {"M": 1}, {"M": 0}, delay=60)
        self.assertEqual(out[0], [(1 - .42, .42, 1.0)])

    def test_read_only(self):
        with self.assertRaises(RuntimeError):
            p.get("https://clob.polymarket.com/order")
        with self.assertRaises(RuntimeError):
            p.get("https://data-api.polymarket.com.evil.example/trades")


if __name__ == "__main__":
    unittest.main()
