import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meme_rec as m

POOL = {"id": "solana_P1", "attributes": {
    "address": "P1", "name": "DOG / SOL", "pool_created_at": "2026-11-27T10:00:00Z",
    "reserve_in_usd": "52000.5", "base_token_price_usd": "0.0012", "fdv_usd": "1200000",
    "volume_usd": {"m5": "30000", "h1": "90000"}, "price_change_percentage": {"m5": "12.5", "h1": "80"},
    "transactions": {"m5": {"buys": 120, "sells": 30, "buyers": 90, "sellers": 25}},
    "locked_liquidity_percentage": None},
    "relationships": {"base_token": {"data": {"id": "solana_TOK"}}, "dex": {"data": {"id": "pumpswap"}}}}


class TestMemeRec(unittest.TestCase):
    def test_parse(self):
        p = m.pool_row(POOL, 1790000000)
        self.assertEqual((p["addr"], p["token"], p["dex"]), ("P1", "TOK", "pumpswap"))
        self.assertEqual(p["created"], 1795773600)
        self.assertAlmostEqual(p["reserve"], 52000.5)
        self.assertEqual((p["buys_m5"], p["sells_m5"]), (120, 30))

    def test_missing_is_none_not_zero(self):
        p = m.pool_row({"attributes": {"address": "X"}}, 0)
        self.assertIsNone(p["reserve"])
        self.assertIsNone(p["vol_m5"])
        self.assertIsNone(p["locked"])
        self.assertIsNone(p["created"])

    def test_universe(self):
        p = m.pool_row(POOL, 0)
        now = p["created"] + 3600
        self.assertTrue(m.universe(p, now))
        self.assertFalse(m.universe(p, p["created"] + 7 * 3600))   # older than 6h
        q = dict(p, reserve=39_999.0)
        self.assertFalse(m.universe(q, now))
        q = dict(p, vol_m5=None)
        self.assertFalse(m.universe(q, now))

    def test_token(self):
        r = m.token_row({"data": {"attributes": {"holders": {"count": 412,
            "distribution_percentage": {"top_10": "28.4"}}, "mint_authority": "no",
            "freeze_authority": None}}}, 5, "TOK")
        self.assertEqual((r["holders"], r["top10"], r["mint_auth"]), (412, 28.4, "no"))

    def test_read_only(self):
        with self.assertRaises(RuntimeError):
            m.assert_read_only("/networks/solana/swap")
        m.assert_read_only("/networks/solana/pools/multi/A,B")

    def test_client_stops_at_its_deadline_without_calling(self):
        import time
        c = m.Client(log=lambda *x: None)
        c.deadline = time.time() - 1
        self.assertIsNone(c.get("/networks/solana/new_pools", {"page": 1}))
        self.assertEqual(c.n, 0)

    def test_a_killed_run_still_saves_its_state(self):
        import tempfile, json
        d = tempfile.mkdtemp()
        st = os.path.join(d, "state.json")
        m.save_state(st, {"pools": {"A": {"created": 1, "token": "t", "last": 0}}, "tok": {}})
        self.assertEqual(json.load(open(st))["pools"]["A"]["token"], "t")
        self.assertFalse(os.path.exists(st + ".tmp"))


if __name__ == "__main__":
    unittest.main()


class TestMemeScore(unittest.TestCase):
    def setUp(self):
        import meme_score
        self.s = meme_score

    def test_stop_gaps_to_the_low(self):
        c = [(0, 1.0, 1.0, 1.0, 1.0), (60, 1.0, 1.0, 0.5, 0.6)]
        r = self.s.simulate(c, -30)
        self.assertLess(r["x"], 0.6)      # filled at the low, not at -12%
        self.assertFalse(r["rug"])

    def test_stop_before_target_in_one_bucket(self):
        c = [(0, 1.0, 1.0, 1.0, 1.0), (60, 1.0, 3.0, 0.8, 2.5)]
        r = self.s.simulate(c, -30)
        self.assertLess(r["x"], 1.0)

    def test_take_profits_then_time_stop(self):
        c = [(0, 1.0, 1.0, 1.0, 1.0)] + [(60 * i, 2.2, 2.2, 2.1, 2.2) for i in range(1, 40)]
        r = self.s.simulate(c, -30)
        self.assertGreater(r["x"], 1.5)

    def test_rug_zeroes_the_rest(self):
        c = [(0, 1.0, 1.0, 1.0, 1.0), (60, 1.0, 1.0, 0.05, 0.05)]
        r = self.s.simulate(c, -30)
        self.assertTrue(r["rug"])
        self.assertEqual(r["x"], 0.0)

    def test_entry_never_uses_the_signal_bucket(self):
        c = [(0, 0.5, 0.5, 0.5, 0.5), (60, 1.0, 1.0, 1.0, 1.0)]
        r = self.s.simulate(c, 10)        # signal at 10s, latency 30 -> first start >= 40 is 60
        self.assertAlmostEqual(r["x"], 1.0 * (1 - self.s.SLIP_OUT) / (1 + self.s.SLIP_IN), places=6)
