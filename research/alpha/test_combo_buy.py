import gzip, json, math, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import combo_buy as CB     # noqa: E402
import cheap_screen as C   # noqa: E402

T0 = 1_800_000_000


def candle(end, bid, ask):
    return {"end_period_ts": end, "yes_bid": {"close_dollars": str(bid)},
            "yes_ask": {"close_dollars": str(ask)}}


class TestLegPrice(unittest.TestCase):
    def test_no_lookahead(self):
        cs = CB.parse_candles({"candlesticks": [candle(T0 - 60, .40, .42), candle(T0 + 60, .90, .92)]})
        p, age = CB.leg_prob_at(cs, "yes", T0)
        self.assertAlmostEqual(p, .41)
        self.assertEqual(age, 60)

    def test_candle_ending_exactly_at_t_is_usable(self):
        cs = CB.parse_candles({"candlesticks": [candle(T0, .20, .22)]})
        self.assertAlmostEqual(CB.leg_prob_at(cs, "yes", T0)[0], .21)

    def test_stale_price_is_none(self):
        cs = CB.parse_candles({"candlesticks": [candle(T0 - CB.LEG_MAX_AGE - 1, .40, .42)]})
        self.assertEqual(CB.leg_prob_at(cs, "yes", T0), (None, None))

    def test_empty_book_sentinels_are_none(self):
        cs = CB.parse_candles({"candlesticks": [candle(T0 - 60, 0.0, 1.0)]})
        self.assertEqual(cs, [(T0 - 60, None, None)])
        self.assertEqual(CB.leg_prob_at(cs, "yes", T0), (None, None))

    def test_no_side_flips(self):
        cs = CB.parse_candles({"candlesticks": [candle(T0 - 60, .70, .72)]})
        self.assertAlmostEqual(CB.leg_prob_at(cs, "no", T0)[0], .29)


def legs(*spec):
    return [{"market_ticker": m, "event_ticker": e, "side": s} for m, e, s in spec]


class TestFeatures(unittest.TestCase):
    L = legs(("KXNFLGAME-26NOV23KCDEN-KC", "KXNFLGAME-26NOV23KCDEN", "yes"),
             ("KXNFLSPREAD-26NOV23KCDEN-KC3", "KXNFLSPREAD-26NOV23KCDEN", "yes"),
             ("KXNBAGAME-26NOV23LALBOS-LAL", "KXNBAGAME-26NOV23LALBOS", "no"))
    CATS = {"KXNFLGAME": "Sports", "KXNFLSPREAD": "Sports", "KXNBAGAME": "Sports"}

    def cand(self, p):
        return [(T0 - 120, p - .01, p + .01), (T0 - 3600 - 60, p / 2 - .01, p / 2 + .01)]

    def test_same_game_and_gap(self):
        lc = {self.L[0]["market_ticker"]: self.cand(.5), self.L[1]["market_ticker"]: self.cand(.4),
              self.L[2]["market_ticker"]: self.cand(.5)}
        close = {l["market_ticker"]: T0 + 7200 for l in self.L}
        f = CB.features(self.L, close, lc, 0.05, T0, self.CATS)
        self.assertEqual((f["n_legs"], f["n_games"], f["same_game_pairs"], f["has_same_game"]), (3, 2, 1, 1))
        indep = .5 * .4 * (1 - .5)
        self.assertAlmostEqual(f["gap"], math.log(.05 / indep))
        self.assertLess(f["gap"], 0)             # combo costs less than its legs multiplied out
        self.assertEqual(f["cat_mix"], "Sports")
        self.assertAlmostEqual(f["ttc_first_h"], 2.0)
        self.assertAlmostEqual(f["leg_age_max_m"], 2.0)

    def test_leg_move_reads_the_hour_before(self):
        lc = {l["market_ticker"]: [(T0 - 60, .59, .61), (T0 - 3660, .29, .31)] for l in self.L[:2]}
        f = CB.features(self.L[:2], {}, lc, 0.05, T0, self.CATS)
        self.assertAlmostEqual(f["d_indep60"], math.log((.6 * .6) / (.3 * .3)))

    def test_one_missing_leg_is_none_not_zero(self):
        lc = {self.L[0]["market_ticker"]: self.cand(.5)}
        f = CB.features(self.L, {}, lc, 0.05, T0, self.CATS)
        self.assertIsNone(f["gap"]); self.assertIsNone(f["d_indep60"]); self.assertIsNone(f["leg_age_max_m"])
        self.assertIsNone(f["ttc_first_h"])

    def test_no_legs(self):
        f = CB.features([], {}, {}, 0.05, T0, {})
        self.assertIsNone(f["has_same_game"]); self.assertIsNone(f["cat_mix"])


class TestBuys(unittest.TestCase):
    def test_first_cheap_yes_buy_only(self):
        tr = [("KXMVEA-1-X", T0 + 50, "yes", .08, 10), ("KXMVEA-1-X", T0 + 10, "yes", .10, 3),
              ("KXMVEA-2-X", T0, "no", .05, 9), ("KXMVEA-3-X", T0, "yes", .20, 9),
              ("KXNFLGAME-1-X", T0, "yes", .05, 9), None]
        b = CB.first_cheap_buys(tr, 100)
        self.assertEqual(b, {"KXMVEA-1-X": (T0 + 10, .10, 3)})

    def test_cap_is_by_hash_not_by_order(self):
        tr = [(f"KXMVEA-{i}-X", T0 + i, "yes", .05, 1) for i in range(50)]
        a = set(CB.first_cheap_buys(tr, 10)); b = set(CB.first_cheap_buys(list(reversed(tr)), 10))
        self.assertEqual(a, b); self.assertEqual(len(a), 10)

    def test_grid_shards_partition_the_sample(self):
        allh = CB.grid(T0 // 3600 * 3600, 30, 0, 1)
        parts = [set(CB.grid(T0 // 3600 * 3600, 30, s, 3)) for s in range(3)]
        self.assertEqual(set().union(*parts), set(allh))
        self.assertEqual(sum(map(len, parts)), len(allh))
        self.assertTrue(all((h // 3600) % CB.STRIDE == 0 for h in allh))


class TestExport(unittest.TestCase):
    def test_round_trip_into_cheap_screen(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "x.db"); c = CB.db(path)
        c.execute("INSERT INTO meta VALUES('t_end', ?)", (str(T0 // 3600 * 3600),))
        f = CB.features(TestFeatures.L, {}, {}, .05, T0, TestFeatures.CATS)
        f.update(size=math.log1p(20), hour_et=9)
        for tk, res in (("KXMVEA-1-A", "yes"), ("KXMVEA-1-B", "no"), ("KXMVEA-1-C", None)):
            c.execute("INSERT INTO crow VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (tk, T0, T0, .05, 20, "KXMVEA", "KXMVEA-1", T0 + 86400, res, json.dumps(f)))
        c.commit()
        out = os.path.join(d, "combo", "x-0.csv.gz")
        self.assertEqual(CB.export(path, out, 30, 0, 1), 2)       # the unsettled combo is not a row
        rows = list(C.iter_rows([out]))
        self.assertEqual({r["source"] for r in rows}, {"combo"})
        self.assertEqual(sorted(r["won"] for r in rows), [0, 1])
        self.assertEqual(rows[0]["f"]["same_game_pairs"], 1.0)
        self.assertIsNone(rows[0]["f"]["gap"])
        self.assertEqual(rows[0]["f"]["last_cheap"], 1)
        cov = open(out.replace(".csv.gz", "-coverage.txt")).read()
        self.assertIn("unsettled 1", cov)
        self.assertIn(f"unread {len(CB.grid(T0 // 3600 * 3600, 30, 0, 1))}", cov)

    def test_read_only(self):
        CB.T.assert_read_only("/series/KXNFLGAME/markets/KXNFLGAME-26NOV23KCDEN-KC/candlesticks")
        with self.assertRaises(RuntimeError):
            CB.T.assert_read_only("/portfolio/orders")


if __name__ == "__main__":
    unittest.main()
