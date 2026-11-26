"""Freezes idea #18's zero-lookahead rules: which TSA days count as published at a
trade time, that the week window is chosen by reproducing Kalshi's own figure,
and the event-date parser."""
import os, sys, unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tsa_model as M  # noqa: E402

ET = ZoneInfo("America/New_York")


def ts(y, mo, d, h):
    return datetime(y, mo, d, h, tzinfo=ET).timestamp()


class Published(unittest.TestCase):
    def test_a_weekday_is_known_from_noon_the_next_business_day(self):
        # Tue 11-10 11:00: Mon 11-09 not yet; the weekend was posted Monday noon
        self.assertEqual(M.known_days(ts(2026, 11, 10, 11)), date(2026, 11, 8))
        self.assertEqual(M.known_days(ts(2026, 11, 10, 12)), date(2026, 11, 9))

    def test_the_weekend_waits_for_monday(self):
        # Sunday 11-15 18:00: Friday 11-13 is posted Monday, so only Thursday is known
        self.assertEqual(M.known_days(ts(2026, 11, 15, 18)), date(2026, 11, 12))
        self.assertEqual(M.known_days(ts(2026, 11, 16, 12)), date(2026, 11, 15))

    def test_a_holiday_delays_publication(self):
        # Thanksgiving 2026 is Thu 11-26: Wednesday's count waits until Friday noon
        self.assertIn(date(2026, 11, 26), M.federal_holidays(2026))
        self.assertEqual(M.known_days(ts(2026, 11, 26, 15)), date(2026, 11, 24))
        self.assertEqual(M.known_days(ts(2026, 11, 27, 12)), date(2026, 11, 26))

    def test_forecast_never_uses_an_unpublished_day(self):
        tsa = {date(2025, 1, 1) + timedelta(k): 2_000_000 for k in range(800)}
        # poison the days that are not yet public: a leak would move the mean
        wk = M.WINDOWS["7d ending on the date"](date(2026, 11, 15))
        mark = ts(2026, 11, 12, 13)          # Thu 13:00 ET: Mon-Wed known
        for d in wk:
            if d > M.known_days(mark):
                tsa[d] = 9_000_000
        mean, k, rr = M.forecast(wk, mark, tsa)
        self.assertEqual(k, 4)
        self.assertAlmostEqual(mean, 2_000_000, delta=1)


class Window(unittest.TestCase):
    def test_the_window_that_grades_kalshis_strikes_wins(self):
        tsa = {date(2026, 1, 1) + timedelta(k): 1_000_000 + 1000 * k for k in range(200)}
        ms = []
        for i in range(20):
            d = date(2026, 3, 1) + timedelta(7 * i)
            days = M.WINDOWS["7d ending on the date"](d)
            avg = sum(tsa[x] for x in days) / 7
            ev = "KXTSAW-26" + ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG"][d.month - 1] \
                 + f"{d.day:02d}"
            for k in (avg - 2500, avg + 2500):
                ms.append({"event_ticker": ev, "strike_type": "greater", "floor_strike": str(k),
                           "result": "yes" if avg > k else "no"})
        best = M.pick_window(ms, tsa, lambda s: None)
        self.assertEqual(best[0], "7d ending on the date")
        self.assertEqual(best[1], 1.0)


class Parse(unittest.TestCase):
    def test_event_dates(self):
        self.assertEqual(M.event_date("KXTSAW-26NOV15"), date(2026, 11, 15))
        self.assertEqual(M.event_date("TSAW-21JUL17"), date(2021, 7, 17))
        self.assertIsNone(M.event_date("KXTSAW"))

    def test_taker_fee_rounds_up_per_ten_lot(self):
        # ceil(0.07 x 10 x 0.5 x 0.5 = 17.5c) = 18c per 10 = 1.8c
        self.assertAlmostEqual(M.taker_fee(0.5), 0.018)


if __name__ == "__main__":
    unittest.main()
