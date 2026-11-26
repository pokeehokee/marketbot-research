import lzma, os, struct, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fx_lead as F


def bi5(rows):
    return lzma.compress(b"".join(struct.pack(">IIIff", *r) for r in rows), format=lzma.FORMAT_ALONE)


class TestBi5(unittest.TestCase):
    def test_mid_and_time(self):
        pts = F.parse_bi5(bi5([(1500, 108012, 108010, 1.0, 1.0)]), 3600, 1e5)
        self.assertEqual(len(pts), 1)
        self.assertAlmostEqual(pts[0][0], 3601.5)
        self.assertAlmostEqual(pts[0][1], 1.08011)

    def test_empty_hour(self):
        self.assertEqual(F.parse_bi5(b"", 0, 1e5), [])

    def test_url_month_is_zero_based(self):
        # 2026-11-25 14:00 UTC -> month folder 10
        self.assertTrue(F.duka_url("EURUSD", 1795615200).endswith("/EURUSD/2026/10/25/14h_ticks.bi5"))


class TestNoLookahead(unittest.TestCase):
    def setUp(self):
        self.t = F.Ticks("EURUSD", 1e5, print)
        self.t.hours = {3600: ([3600.0, 3700.0], [1.1, 1.2]), 7200: ([7300.0], [1.3])}

    def test_last_tick_at_or_before(self):
        self.assertEqual(self.t.at(3699.9), 1.1)
        self.assertEqual(self.t.at(3700.0), 1.2)

    def test_falls_back_to_previous_hour(self):
        self.assertEqual(self.t.at(7250.0, max_age=10000), 1.2)

    def test_stale_tick_is_none(self):
        self.assertIsNone(self.t.at(7250.0, max_age=60))

    def test_before_any_tick(self):
        self.assertIsNone(self.t.at(3500.0))


if __name__ == "__main__":
    unittest.main()
