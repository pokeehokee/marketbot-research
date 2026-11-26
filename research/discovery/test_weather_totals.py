"""Freezes #17's load-bearing rules: event parsing, team matching, grading a `greater` strike,
the weather hour never at or after kickoff minus an hour, and the read-only weather guard."""
import os, sys, unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weather_totals as W  # noqa: E402


class Parse(unittest.TestCase):
    def test_event_ticker(self):
        self.assertEqual(W.ev_parse("KXNFLTOTAL-25SEP07BUFBAL"), (date(2025, 9, 7), "BUFBAL"))
        self.assertEqual(W.ev_parse("garbage"), (None, None))
        # Miami (OH): the team code carries a hyphen, and the 2026-11-24 run crashed on it.
        self.assertEqual(W.ev_parse("KXNCAAFTOTAL-25DEC27M-OHFRES"), (date(2025, 12, 27), "M-OHFRES"))
        self.assertEqual(W.ev_parse("KXNCAAFTOTAL-25NOV29BALLM-OH"), (date(2025, 11, 29), "BALLM-OH"))


class Match(unittest.TestCase):
    g = {"away_abbr": "WSH", "home_abbr": "NYG", "away_names": ["Washington Commanders", "Commanders"],
         "home_names": ["New York Giants", "Giants"]}

    def test_abbreviation_pair_with_alias(self):
        self.assertTrue(W.matches("WASNYG", "", self.g))

    def test_both_teams_needed_by_name(self):
        self.assertTrue(W.matches("ZZZ", "Commanders at Giants: Total Points", self.g))
        self.assertFalse(W.matches("ZZZ", "Commanders at Eagles: Total Points", self.g))


class Grade(unittest.TestCase):
    def test_greater_strike(self):
        self.assertTrue(W.total_over({"floor_strike": "44.5"}, 45))
        self.assertFalse(W.total_over({"floor_strike": "45"}, 45))


class NoLookahead(unittest.TestCase):
    def test_weather_hour_at_least_an_hour_before_kickoff(self):
        for k in (1758000000, 1758003599, 1758003600, 1758005400):
            lab = W.weather_hour(k).timestamp()
            self.assertLessEqual(lab, k - 3600)
            self.assertGreater(lab, k - 7200)


class Guard(unittest.TestCase):
    def test_only_the_two_weather_reads(self):
        with self.assertRaises(RuntimeError):
            W.om_get("api.open-meteo.com", "/v1/forecast", {})
        with self.assertRaises(RuntimeError):
            W.om_get("example.com", "/v1/archive", {})


class Geo(unittest.TestCase):
    res = [{"admin1": "Georgia", "latitude": 33.9, "longitude": -83.4},
           {"admin1": "Ohio", "latitude": 39.3, "longitude": -82.1}]

    def test_postal_code_matches_full_state_name(self):
        self.assertEqual(W.pick_geo(self.res, "OH"), (39.3, -82.1))
        self.assertEqual(W.pick_geo(self.res, "Ohio"), (39.3, -82.1))

    def test_no_state_match_is_no_location_not_the_first_result(self):
        self.assertIsNone(W.pick_geo(self.res, "KY"))
        self.assertIsNone(W.pick_geo(self.res, None))
        self.assertIsNone(W.pick_geo([], "OH"))


class WindLookup(unittest.TestCase):
    def test_reads_the_named_hour_only(self):
        from datetime import datetime, timezone
        h = {"time": ["2025-10-05T16:00", "2025-10-05T17:00"], "wind_speed_10m": [7.0, None]}
        labs = {"a": datetime(2025, 10, 5, 16, tzinfo=timezone.utc),
                "b": datetime(2025, 10, 5, 17, tzinfo=timezone.utc),
                "c": datetime(2025, 10, 5, 18, tzinfo=timezone.utc)}
        self.assertEqual(W.wind_lookup(h, labs), {"a": 7.0})


class Fee(unittest.TestCase):
    def test_taker_fee_at_half(self):
        self.assertAlmostEqual(W.taker_fee(0.5), 0.018)


if __name__ == "__main__":
    unittest.main()
