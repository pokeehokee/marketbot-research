import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wx_index as W
import taker_flow_screen as T


class TestNoLookahead(unittest.TestCase):
    def test_point_is_unknown_until_its_publish_lag_has_passed(self):
        pts = [(1000, 50.0), (1060, 51.0), (1120, 52.0)]
        self.assertEqual(W.known_at(pts, 1060 + 299), (1000, 50.0))
        self.assertEqual(W.known_at(pts, 1060 + 300), (1060, 51.0))
        self.assertIsNone(W.known_at(pts, 1000 + 299))

    def test_missing_value_is_skipped_not_zero(self):
        pts = [(1000, 50.0), (1060, None)]
        self.assertEqual(W.known_at(pts, 5000), (1000, 50.0))

    def test_order_does_not_matter(self):
        self.assertEqual(W.known_at([(1120, 52.0), (1000, 50.0)], 9999), (1120, 52.0))


class TestSettlementValue(unittest.TestCase):
    def test_greatest_minute_at_or_before_settlement(self):
        pts = [(3600 * 10 - 60, 40.0), (3600 * 10, 41.0), (3600 * 10 + 60, 99.0)]
        self.assertEqual(W.settlement_value(pts, 3600 * 10), (36000, 41.0))

    def test_older_than_sixty_minutes_is_not_eligible(self):
        self.assertIsNone(W.settlement_value([(36000 - 3601, 40.0)], 36000))


class TestStrikes(unittest.TestCase):
    def test_greater_is_strict(self):
        self.assertEqual(W.implied_result("greater", 45.99, None, 45.99), "no")
        self.assertEqual(W.implied_result("greater", 45.99, None, 46.0), "yes")

    def test_between_and_unknown(self):
        self.assertEqual(W.implied_result("between", 40, 41.99, 41.0), "yes")
        self.assertIsNone(W.implied_result("weird", 1, 2, 3))
        self.assertIsNone(W.implied_result("greater", 1, None, None))


class TestTicker(unittest.TestCase):
    def test_noon_new_york(self):
        # 2026-11-25 12:00 EST = 17:00 UTC
        self.assertEqual(W.settle_ts_from_ticker("KXTEMPNYCHS-26NOV2512-T45.99"), 1795626000)

    def test_unparseable(self):
        self.assertIsNone(W.settle_ts_from_ticker("KXTEMPNYCHS"))


class TestReadOnly(unittest.TestCase):
    def test_weather_path_allowed_writes_refused(self):
        T.assert_read_only("/live_data/weather/miami?last_sec=60")
        with self.assertRaises(RuntimeError):
            T.assert_read_only("/portfolio/orders")


if __name__ == "__main__":
    unittest.main()
