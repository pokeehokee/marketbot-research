import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import combo_tape as C  # noqa: E402
import score_combo as S  # noqa: E402
import taker_flow_screen as T  # noqa: E402

H = 1_763_000_000 // 3600 * 3600


def trade(tk, side, yes_price, count, ts):
    return {"ticker": tk, "taker_outcome_side": side, "yes_price_dollars": f"{yes_price:.4f}",
            "count_fp": f"{count:.2f}", "created_time": __import__("time").strftime(
                "%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime(ts))}


class FakeGet:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        i = int((params or {}).get("cursor") or 0)
        nxt = str(i + 1) if i + 1 < len(self.pages) else None
        return {"trades": self.pages[i], "cursor": nxt}, None


class ComboTapeTest(unittest.TestCase):
    def setUp(self):
        self._get = T.get

    def tearDown(self):
        T.get = self._get

    def test_fees_follow_the_schedule_and_round_up(self):
        # 100 contracts at 0.25: maker 0.035*100*.25*.75 = 0.65625 -> 0.66; taker 1.3125 -> 1.32
        self.assertAlmostEqual(C.fee_ceil(C.MAKER_K, 100, .25), 0.66)
        self.assertAlmostEqual(C.fee_ceil(C.TAKER_K, 100, .25), 1.32)

    def test_only_combo_tickers_are_kept_and_the_hash_is_stable(self):
        self.assertTrue(C.is_combo("KXMVECROSSCATEGORY-S1-ABC"))
        self.assertFalse(C.is_combo("KXBTCD-26NOV2414-T90000"))
        self.assertEqual([C.kept(f"KXMVE-{i}", 25) for i in range(50)],
                         [C.kept(f"KXMVE-{i}", 25) for i in range(50)])
        share = sum(C.kept(f"KXMVE-X-{i}", 25) for i in range(4000)) / 4000
        self.assertTrue(0.21 < share < 0.29, share)

    def test_a_NO_buyer_is_priced_off_the_NO_side(self):
        tk = next(f"KXMVEX-1-{i}" for i in range(100) if C.kept(f"KXMVEX-1-{i}", 25))
        T.get = FakeGet([[trade(tk, "no", 0.30, 10, H + 5), trade("KXBTCD-A", "yes", .5, 1, H + 6)]])
        _, comp, pages, combo, other = C.fetch_hour(H, None, 25, print)
        self.assertEqual((combo, other, pages), (1, 1, 1))
        (key, v), = comp.items()
        self.assertEqual(key, (tk, "no", 0.7))
        self.assertAlmostEqual(v[0], 10)
        self.assertAlmostEqual(v[3], C.fee_ceil(C.TAKER_K, 10, .7))

    def test_a_failed_page_is_not_a_quiet_hour(self):
        T.get = lambda path, params=None: (None, "HTTP 500")
        self.assertIsNone(C.fetch_hour(H, None, 25, lambda s: None)[1])

    def _agg(self, comp, markets):
        d = tempfile.mkdtemp()
        c = C.db(os.path.join(d, "t.db"))
        c.execute("INSERT INTO cfee VALUES('KXMVEX','quadratic_with_combo_maker_fees',NULL,'{}')")
        return c, C.aggregate(c, H, comp, 1, 3, 0, markets), d

    def test_unsettled_markets_are_counted_never_graded(self):
        comp = {("KXMVEX-1-A", "yes", .2): [5, 1, 0.03, 0.06, H + 10],
                ("KXMVEX-1-B", "yes", .2): [5, 1, 0.03, 0.06, H + 10]}
        markets = {"KXMVEX-1-A": ("E", "KXMVEX", H + 7200, "no"),
                   "KXMVEX-1-B": ("E", "KXMVEX", H + 7200, None)}
        c, (s, u), _ = self._agg(comp, markets)
        self.assertEqual((s, u), (1, 1))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM ccell").fetchone()[0], 1)

    def test_a_trade_after_close_is_dropped(self):
        comp = {("KXMVEX-1-A", "yes", .2): [5, 1, 0.03, 0.06, H + 9000]}
        c, (s, u), _ = self._agg(comp, {"KXMVEX-1-A": ("E", "KXMVEX", H + 7200, "yes")})
        self.assertEqual(s, 0)

    def test_shards_are_disjoint_and_cover_every_hour(self):
        now = H + 40 * 86400
        _, _, all_h = C.hours_for(now, 28, 0, 1)
        a = C.hours_for(now, 28, 0, 2)[2]; b = C.hours_for(now, 28, 1, 2)[2]
        self.assertEqual(sorted(a + b), sorted(all_h)); self.assertFalse(set(a) & set(b))
        self.assertTrue(45 <= len(all_h) <= 55)
        self.assertEqual(len({(h // 3600) % 24 for h in all_h}), 24)
        self.assertLess(max(all_h), now - C.SETTLE_LAG_D * 86400)

    def test_export_round_trips_into_the_scorer_and_the_arithmetic_holds(self):
        # Quoter sold YES at 0.20 on 10 contracts in two markets; one hit (taker won).
        comp = {("KXMVEX-1-A", "yes", .2): [10, 1, 0.06, 0.12, H + 10],
                ("KXMVEX-1-B", "yes", .2): [10, 1, 0.06, 0.12, H + 10],
                ("KXMVEX-1-C", "no", .7): [10, 1, 0.08, 0.15, H + 10]}
        markets = {"KXMVEX-1-A": ("E", "KXMVEX", H + 7200, "yes"),
                   "KXMVEX-1-B": ("E", "KXMVEX", H + 7200, "no"),
                   "KXMVEX-1-C": ("E", "KXMVEX", H + 7200, "no")}
        c, _, d = self._agg(comp, markets)
        out = os.path.join(d, "x.csv.gz")
        C.export(os.path.join(d, "t.db"), out)
        rows = S.load([out])
        sell = S.day_cells(rows, lambda r: "k" if r["taker_side"] == "yes" else None, "sell")["k"]
        (day, v), = sell.items()
        # quoter: paid 4.00 in, paid out 10 on A -> gross -6.00, fees 0.12
        self.assertAlmostEqual(v[1], -6.0); self.assertAlmostEqual(v[2], -6.12)
        self.assertEqual((v[5], v[6]), (1, 2))
        buy = S.day_cells(rows, lambda r: "k" if r["taker_side"] == "no" else None, "buy")["k"]
        (day, v), = buy.items()
        # NO buyer paid 7.00 for 10 NO, which won: +3.00 gross, taker fee 0.15
        self.assertAlmostEqual(v[1], 3.0); self.assertAlmostEqual(v[2], 2.85)
        self.assertEqual((v[3], v[5]), (0.0, 0))

    def test_gate_needs_both_halves_and_30_losses(self):
        good = {f"2026-11-{d:02d}": [100, 10, 9, 5, 10, 2, 20] for d in range(1, 29)}
        ok, _ = S.verdict(good, "2026-11-15")
        self.assertTrue(ok)
        thin = {k: [100, 10, 9, 5, 10, 1, 20] for k in list(good)[:20]}
        self.assertFalse(S.verdict(thin, "2026-11-11")[0])
        bad = dict(good); bad.update({f"2026-11-{d:02d}": [100, -10, -11, 15, 10, 9, 20]
                                      for d in range(15, 29)})
        self.assertFalse(S.verdict(bad, "2026-11-15")[0])


if __name__ == "__main__":
    unittest.main()
