"""Freezes the sports test's pricing rules: American odds, the vig removal, the
fee, the empty-book sentinel and the no-lookahead candle choice."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sports_lines as S  # noqa: E402
import taker_flow_screen as T  # noqa: E402


class Odds(unittest.TestCase):
    def test_american(self):
        self.assertAlmostEqual(S.american_to_prob("-150"), 0.6)
        self.assertAlmostEqual(S.american_to_prob("+130"), 100 / 230)
        self.assertEqual(S.american_to_prob("EVEN"), 0.5)
        self.assertIsNone(S.american_to_prob("+50"))
        self.assertIsNone(S.american_to_prob(None))

    def test_novig_sums_to_one(self):
        ph, pa = S.american_to_prob("-150"), S.american_to_prob("+130")
        p = S.novig(ph, pa)
        self.assertAlmostEqual(p + S.novig(pa, ph), 1.0)
        self.assertLess(p, ph)

    def test_both_espn_shapes(self):
        a = {"provider": {"name": "DK"}, "moneyline": {"home": {"close": {"odds": "-120"}},
                                                         "away": {"close": {"odds": "+100"}}}}
        b = {"provider": {"name": "DK"}, "homeTeamOdds": {"moneyLine": -120},
             "awayTeamOdds": {"moneyLine": 100}}
        self.assertEqual(S.odds_block_lines(a)[:2], ("-120", "+100"))
        self.assertEqual(S.odds_block_lines(b)[:2], (-120, 100))

    def test_espn_time_without_seconds(self):
        self.assertEqual(S.espn_ts("2026-11-24T00:00Z"), T.utc("2026-11-24T00:00:00Z"))
        self.assertEqual(S.espn_ts("2026-11-24T00:00:30Z"), T.utc("2026-11-24T00:00:30Z"))

    def test_fee_rounds_up_per_order(self):
        # ceil(0.07 x 10 x 0.5 x 0.5) = ceil(17.5c) = 18c per 10 -> 1.8c/ct
        self.assertAlmostEqual(S.taker_fee(0.5, 1.0), 0.018)


class Quote(unittest.TestCase):
    def test_candle_must_end_before_start_and_sentinels_are_dropped(self):
        start = 1_000_000
        payload = {"candlesticks": [
            {"end_period_ts": start - 120, "yes_ask": {"close_dollars": "0.55"},
             "yes_bid": {"close_dollars": "0.53"}},
            {"end_period_ts": start + 60, "yes_ask": {"close_dollars": "0.90"},
             "yes_bid": {"close_dollars": "0.88"}},
        ]}
        orig = T.get; T.get = lambda p, q=None: (payload, None)
        try:
            ask, bid, age = S.quote_at("S", "X", start)
        finally:
            T.get = orig
        self.assertEqual((ask, bid, age), (0.55, 0.53, 120))
        payload["candlesticks"] = [{"end_period_ts": start, "yes_ask": {"close_dollars": "1.00"},
                                    "yes_bid": {"close_dollars": "0.00"}}]
        T.get = lambda p, q=None: (payload, None)
        try:
            self.assertEqual(S.quote_at("S", "X", start)[:2], (None, None))
        finally:
            T.get = orig

    def test_espn_whitelist(self):
        with self.assertRaises(RuntimeError):
            S.espn_get("/apis/site/v2/sports/football/nfl/teams")


class Matching(unittest.TestCase):
    def test_event_maps_to_the_one_game_with_both_teams(self):
        def tm(loc, name, abbr):
            return {"abbr": abbr, "loc": loc, "name": name, "short": name, "disp": f"{loc} {name}"}
        start = T.utc("2026-11-23T00:30:00Z")   # Nov 22, 7:30 PM ET
        g = {"id": "1", "start": start, "home": tm("Boston", "Celtics", "BOS"),
             "away": tm("Los Angeles", "Lakers", "LAL")}
        other = {"id": "2", "start": start, "home": tm("Los Angeles", "Clippers", "LAC"),
                 "away": tm("Denver", "Nuggets", "DEN")}
        ev = {"E": [{"ticker": "E-LAL", "yes_sub_title": "Los Angeles L", "close_time": "2026-11-23T03:00:00Z"},
                    {"ticker": "E-BOS", "yes_sub_title": "Boston", "close_time": "2026-11-23T03:00:00Z"}]}
        out = S.match(ev, [g, other], lambda s: None, "nba")
        self.assertEqual(out["E-BOS"][1], "home")
        self.assertEqual(out["E-LAL"][0]["id"], "1")


if __name__ == "__main__":
    unittest.main()
